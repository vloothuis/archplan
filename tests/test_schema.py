import json
import os
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import cli, schema  # noqa: E402
from archplan.detectors import (data_cross_fk, data_enum_mismatch, data_flag_columns, data_json_bag,  # noqa: E402
                                data_orphan_table, data_polymorphic, data_repeated_columns, data_shared_table)
from archplan.detectors.common import fingerprint  # noqa: E402
from archplan.schema import access, ddl, django, ecto, live, prisma, rails  # noqa: E402


def write(root: Path, files: dict):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))


def col(t, name):
    return next(c for c in t["columns"] if c["name"] == name)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


TRICKY_SQL = r"""
-- a pg_dump style file
SET statement_timeout = 0;
CREATE TYPE public.order_state AS ENUM ('draft', 'paid');
ALTER TYPE public.order_state ADD VALUE 'sent';
CREATE FUNCTION public.f() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO log VALUES (1); -- a semicolon inside a dollar quote
  RETURN NEW;
END $$;
/* block comment; with a semicolon */
CREATE TABLE public."Order" (
    id bigint NOT NULL,
    "customer id" integer,
    state public.order_state DEFAULT 'draft'::public.order_state NOT NULL,
    total numeric(10,2) DEFAULT 0,
    tags text[] DEFAULT '{}'::text[],
    kind character varying(20) CHECK (kind IN ('a', 'b''c')),
    note text DEFAULT NULL,
    CONSTRAINT order_total_check CHECK ((total >= (0)::numeric))
);
CREATE SEQUENCE public.order_id_seq START WITH 1;
ALTER TABLE ONLY public."Order" ALTER COLUMN id SET DEFAULT nextval('public.order_id_seq'::regclass);
ALTER TABLE ONLY public."Order" ADD CONSTRAINT order_pkey PRIMARY KEY (id);
CREATE TABLE customers (id serial PRIMARY KEY, email citext UNIQUE NOT NULL, old int);
ALTER TABLE ONLY public."Order"
    ADD CONSTRAINT order_customer_fk FOREIGN KEY ("customer id") REFERENCES public.customers(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS order_state_idx ON ONLY public."Order" USING btree (state, total) WHERE (state <> 'draft');
COMMENT ON TABLE public."Order" IS 'Orders; one per checkout';
COMMENT ON COLUMN public."Order".note IS 'free text';
ALTER TABLE customers ADD COLUMN name varchar(80) NOT NULL DEFAULT '', DROP COLUMN old, RENAME COLUMN email TO mail;
CREATE TABLE `items` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `order_id` bigint NOT NULL,
  `size` enum('s','m','l') DEFAULT 's' COMMENT 'shirt size',
  PRIMARY KEY (`id`),
  KEY `items_order` (`order_id`),
  CONSTRAINT `items_fk` FOREIGN KEY (`order_id`) REFERENCES `Order` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='order lines';
CREATE TABLE things (id INTEGER PRIMARY KEY AUTOINCREMENT, parent INTEGER REFERENCES things(id),
                     uid bigint GENERATED ALWAYS AS IDENTITY);
CREATE VIEW v AS SELECT 1;
GRANT SELECT ON customers TO reader;
"""


class DDLTest(unittest.TestCase):
    def test_tricky_input(self):
        r = ddl.parse(TRICKY_SQL)
        self.assertEqual(r["enums"]["order_state"], ["draft", "paid", "sent"])
        self.assertEqual(sorted(r["tables"]), ["Order", "customers", "items", "things"])
        o = r["tables"]["Order"]
        self.assertEqual(o["pk"], ["id"])
        self.assertFalse(col(o, "id")["null"])
        self.assertIn("nextval", col(o, "id")["default"])
        self.assertEqual(col(o, "state")["values"], ["draft", "paid", "sent"])
        self.assertFalse(col(o, "state")["null"])
        self.assertEqual(col(o, "total")["type"], "numeric(10,2)")
        self.assertEqual(col(o, "tags")["type"], "text[]")
        self.assertEqual(col(o, "kind")["values"], ["a", "b'c"])
        self.assertEqual(col(o, "note")["default"], "NULL")
        self.assertEqual(col(o, "note")["comment"], "free text")
        self.assertEqual(o["fks"], [{"columns": ["customer id"], "to": "customers", "to_columns": ["id"],
                                     "on_delete": "SET NULL"}])
        self.assertIn({"columns": ["state", "total"], "unique": True}, o["indexes"])
        self.assertTrue(any("order_total_check" in c for c in o["checks"]))
        self.assertEqual(o["comment"], "Orders; one per checkout")
        c = r["tables"]["customers"]
        self.assertEqual([x["name"] for x in c["columns"]], ["id", "mail", "name"])
        self.assertEqual(c["pk"], ["id"])
        self.assertEqual(col(c, "name")["type"], "varchar(80)")
        i = r["tables"]["items"]
        self.assertEqual(col(i, "size")["values"], ["s", "m", "l"])
        self.assertEqual(col(i, "id")["default"], "autoincrement")
        self.assertEqual(i["fks"][0]["to"], "Order")
        self.assertEqual(i["fks"][0]["on_delete"], "CASCADE")
        self.assertIn({"columns": ["order_id"], "unique": False}, i["indexes"])
        self.assertEqual(i["comment"], "order lines")
        t = r["tables"]["things"]
        self.assertEqual(t["fks"][0]["to"], "things")
        self.assertEqual(col(t, "uid")["default"], "identity")

    def test_bad_statements_are_skipped(self):
        r = ddl.parse("CREATE TABLE (;\nCREATE TABLE ok (a int);\nALTER TABLE;\nCREATE INDEX ON;")
        self.assertEqual(list(r["tables"]), ["ok"])


class DjangoTest(TempDir):
    def test_models(self):
        write(self.root, {
            "manage.py": "",
            "shop/__init__.py": "",
            "shop/apps.py": """\
                from django.apps import AppConfig
                class ShopConfig(AppConfig):
                    name = "shop"
                    label = "store"
            """,
            "shop/constants.py": """\
                from django.db import models
                class Status(models.TextChoices):
                    draft = "draft", "Draft"
                    paid = "paid", "Paid"
            """,
            "shop/models.py": """\
                from django.db import models
                from .constants import Status

                class Stamped(models.Model):
                    created = models.DateTimeField(auto_now_add=True)
                    class Meta:
                        abstract = True

                class Customer(Stamped):
                    email = models.EmailField(max_length=120, unique=True)

                class Tag(models.Model):
                    name = models.CharField(max_length=20)
                    class Meta:
                        db_table = "tags"

                class Order(Stamped):
                    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
                    status = models.CharField(max_length=10, choices=Status.choices, default=Status.draft)
                    size = models.CharField(max_length=1, choices=[("s", "Small"), ("l", "Large")])
                    paid_at = models.DateTimeField(null=True)
                    tags = models.ManyToManyField(Tag)
                    lines = models.ManyToManyField("Product", through="Line")
                    class Meta:
                        unique_together = [("customer", "status")]
                        constraints = [models.CheckConstraint(check=models.Q(size__in=["s", "l"]), name="size_ok")]

                class Product(models.Model):
                    sku = models.CharField(max_length=10, db_index=True)

                class Line(models.Model):
                    order = models.ForeignKey("store.Order", on_delete=models.PROTECT)
                    product = models.ForeignKey(Product, on_delete=models.CASCADE)

                class OrderProxy(Order):
                    class Meta:
                        proxy = True

                class SpecialOrder(Order):
                    note = models.TextField()
            """})
        r = django.read(self.root)
        t = r["tables"]
        self.assertEqual(sorted(t), ["store_customer", "store_line", "store_order", "store_order_tags",
                                     "store_product", "store_specialorder", "tags"])
        o = t["store_order"]
        self.assertEqual(o["pk"], ["id"])
        self.assertEqual(col(o, "customer_id")["type"], "bigint")
        self.assertEqual(col(o, "status")["values"], ["draft", "paid"])
        self.assertEqual(col(o, "size")["values"], ["s", "l"])
        self.assertTrue(col(o, "paid_at")["null"])
        self.assertIn("created", [c["name"] for c in o["columns"]])
        self.assertIn({"columns": ["customer_id", "status"], "unique": True}, o["indexes"])
        self.assertTrue(any(c.startswith("size_ok:") for c in o["checks"]))
        self.assertEqual(o["fks"], [{"columns": ["customer_id"], "to": "store_customer", "to_columns": ["id"],
                                     "on_delete": "CASCADE"}])
        j = t["store_order_tags"]
        self.assertEqual({f["to"] for f in j["fks"]}, {"store_order", "tags"})
        self.assertEqual(r["maps"]["store_order_tags"]["through"], "store_order")
        self.assertEqual(t["store_line"]["fks"][0]["on_delete"], "PROTECT")
        self.assertIn({"columns": ["email"], "unique": True}, t["store_customer"]["indexes"])
        self.assertEqual(col(t["store_customer"], "email")["type"], "varchar(120)")
        s = t["store_specialorder"]
        self.assertEqual(s["pk"], ["order_ptr_id"])
        self.assertEqual(s["fks"][0]["to"], "store_order")
        self.assertEqual(r["maps"]["store_order"], {"class": "Order", "file": "shop/models.py", "line": 17})


ECTO_SCHEMA = '''\
defmodule Shop.Orders.Order do
  @moduledoc """
  Example, not a real schema:

      schema "examples" do
      end
  """
  use Ecto.Schema
  alias Shop.Accounts
  alias Shop.Orders.{Line, Tag}

  @states [:draft, :paid]
  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id

  schema "orders" do
    field(:state, Ecto.Enum, values: @states, default: :draft)
    field :size, Ecto.Enum, values: [small: 1, large: 2]
    field :kind, Ecto.Enum, values: Shop.Orders.Kinds.values()
    field :total, :decimal
    field :meta, :map
    field :secret, :string, virtual: true
    belongs_to :customer, Accounts.Customer
    has_many :lines, Line
    many_to_many :tags, Tag, join_through: "orders_tags"
    embeds_one :address, Shop.Address
    timestamps(type: :utc_datetime)
  end
end
'''


class EctoTest(TempDir):
    def setUp(self):
        super().setUp()
        write(self.root, {
            "mix.exs": "defp deps, do: [{:ecto_sql, \"~> 3.0\"}]\n",
            "lib/shop/orders/order.ex": ECTO_SCHEMA,
            "lib/shop/orders/kinds.ex": "defmodule Shop.Orders.Kinds do\n  def values, do: ~w(gift normal)a\nend\n",
            "lib/shop/accounts/customer.ex": """\
                defmodule Shop.Accounts.Customer do
                  use Ecto.Schema
                  schema "customers" do
                    field :email, :string
                  end
                end
            """,
            "lib/shop/orders/tag.ex": """\
                defmodule Shop.Orders.Tag do
                  use Ecto.Schema
                  schema "tags" do
                    field :name, :string
                  end
                end
            """,
            "priv/repo/migrations/20200101000000_create.exs": '''\
                defmodule Shop.Repo.Migrations.Create do
                  use Ecto.Migration

                  def change do
                    create table(:customers) do
                      add :email, :string, null: false
                      add :old, :string
                      timestamps()
                    end

                    create table(:orders, primary_key: false) do
                      add :id, :binary_id, primary_key: true
                      add :customer_id, references(:customers, on_delete: :delete_all, type: :binary_id),
                        null: false
                      add :state, :string, null: false, default: "draft"
                      add :legacy, :boolean, default: false
                    end

                    create unique_index(:customers, [:email])
                    execute """
                    CREATE TYPE order_size AS ENUM ('small', 'large');
                    """
                  end

                  def down do
                    drop table(:orders)
                  end
                end
            ''',
            "priv/repo/migrations/20200201000000_alter.exs": '''\
                defmodule Shop.Repo.Migrations.Alter do
                  use Ecto.Migration
                  def change do
                    alter table(:customers) do
                      remove :old
                      modify :email, :citext
                    end
                    rename table(:orders), :legacy, to: :archived
                    create index(:orders, [:state])
                  end
                end
            ''',
        })

    def test_schemas(self):
        r = ecto.read_schemas(self.root)
        ecto.resolve_pending(r, self.root, {"Shop.Orders.Kinds": "lib/shop/orders/kinds.ex"})
        t = r["tables"]
        self.assertEqual(sorted(t), ["customers", "orders", "orders_tags", "tags"])
        o = t["orders"]
        self.assertEqual(o["pk"], ["id"])
        self.assertEqual(col(o, "id")["type"], "uuid")
        self.assertEqual(col(o, "state")["values"], ["draft", "paid"])
        self.assertEqual(col(o, "state")["default"], "draft")
        self.assertEqual(col(o, "size")["values"], ["small", "large"])
        self.assertEqual(col(o, "kind")["values"], ["gift", "normal"])
        self.assertEqual(col(o, "meta")["type"], "jsonb")
        self.assertEqual(col(o, "address")["type"], "jsonb")
        self.assertEqual(col(o, "customer_id")["type"], "uuid")
        self.assertNotIn("secret", [c["name"] for c in o["columns"]])
        self.assertIn("inserted_at", [c["name"] for c in o["columns"]])
        self.assertEqual(o["fks"], [{"columns": ["customer_id"], "to": "customers", "to_columns": ["id"]}])
        jt = t["orders_tags"]
        self.assertEqual({f["to"] for f in jt["fks"]}, {"orders", "tags"})
        self.assertEqual(r["maps"]["orders"]["module"], "Shop.Orders.Order")
        self.assertEqual(r["maps"]["orders_tags"]["through"], "orders")

    def test_migrations(self):
        s, n, latest = ecto.read_migrations(self.root)
        self.assertEqual((n, latest), (2, "20200201000000"))
        c = s.tables["customers"]
        self.assertEqual([x["name"] for x in c["columns"]], ["id", "email", "inserted_at", "updated_at"])
        self.assertEqual(col(c, "email")["type"], "citext")
        self.assertFalse(col(c, "email")["null"])
        self.assertIn({"columns": ["email"], "unique": True}, c["indexes"])
        o = s.tables["orders"]
        self.assertEqual(o["pk"], ["id"])
        self.assertEqual(o["fks"][0]["on_delete"], "CASCADE")
        self.assertIn("archived", [x["name"] for x in o["columns"]])
        self.assertEqual(s.enums["order_size"], ["small", "large"])

    def test_build_merges_migrations(self):
        sch = schema.build(self.root, {})
        o = sch["tables"]["orders"]
        self.assertFalse(col(o, "state")["null"])  # from the migration
        self.assertEqual(col(o, "state")["values"], ["draft", "paid"])  # from the schema
        self.assertEqual(o["fks"][0]["on_delete"], "CASCADE")
        self.assertIn("archived", [c["name"] for c in o["columns"]])
        self.assertEqual(sch["sources"][0]["kind"], "ecto")
        self.assertEqual(sch["enums"]["order_size"], ["small", "large"])


class RailsPrismaTest(TempDir):
    def test_discovery_skips_test_and_fixture_trees(self):
        write(self.root, {
            "mix.exs": 'defp deps, do: [{:ecto_sql, "~> 3.0"}]\n',
            "lib/shop/orders/order.ex": ECTO_SCHEMA,
            "exporters/elixir/test/fixture/mix.exs": 'defp deps, do: [{:ecto_sql, "~> 3.0"}]\n',
            "exporters/elixir/test/fixture/lib/shop/orders/order.ex": ECTO_SCHEMA,
            "tests/fixture/schema.sql": "CREATE TABLE fake (id integer);\n",
        })

        self.assertEqual(schema.detect(self.root), [{"kind": "ecto"}])

    def test_rails(self):
        write(self.root, {
            "db/schema.rb": """\
                ActiveRecord::Schema[7.1].define(version: 2024_01_01_000000) do
                  create_enum "mood", ["happy", "sad"]

                  create_table "users", force: :cascade do |t|
                    t.string "email", null: false, default: ""
                    t.enum "mood", enum_type: "mood"
                    t.integer "role", default: 0
                    t.timestamps
                    t.index ["email"], name: "index_users_on_email", unique: true
                  end

                  create_table "comments", id: :uuid do |t|
                    t.references "user", null: false
                    t.references "commentable", polymorphic: true
                    t.jsonb "data"
                  end

                  add_foreign_key "comments", "users", on_delete: :cascade
                end
            """,
            "app/models/user.rb": "class User < ApplicationRecord\n  enum role: { member: 0, admin: 1 }\nend\n",
            "app/models/comment.rb": "class Comment < ApplicationRecord\n  self.table_name = \"comments\"\nend\n",
        })
        self.assertEqual(schema.detect(self.root), [{"kind": "rails", "path": "db/schema.rb"}])
        r = rails.read(self.root, self.root / "db/schema.rb")
        u, c = r["tables"]["users"], r["tables"]["comments"]
        self.assertEqual(u["pk"], ["id"])
        self.assertFalse(col(u, "email")["null"])
        self.assertEqual(col(u, "mood")["values"], ["happy", "sad"])
        self.assertEqual(col(u, "role")["values"], ["member", "admin"])
        self.assertIn({"columns": ["email"], "unique": True}, u["indexes"])
        self.assertEqual(col(c, "id")["type"], "uuid")
        self.assertEqual({x["name"] for x in c["columns"]},
                         {"id", "user_id", "commentable_id", "commentable_type", "data"})
        self.assertEqual(c["fks"], [{"columns": ["user_id"], "to": "users", "to_columns": ["id"],
                                     "on_delete": "CASCADE"}])
        self.assertEqual(r["maps"]["users"]["class"], "User")
        self.assertEqual(r["maps"]["comments"]["file"], "app/models/comment.rb")

    def test_prisma(self):
        r = prisma.read("""
            // comment
            enum Role {
              USER
              ADMIN
            }
            model User {
              id    Int     @id @default(autoincrement())
              email String  @unique
              role  Role    @default(USER)
              posts Post[]
              @@map("users")
            }
            model Post {
              id       Int    @id @default(autoincrement())
              authorId Int    @map("author_id")
              author   User   @relation(fields: [authorId], references: [id], onDelete: Cascade)
              meta     Json?
              @@index([authorId])
              @@unique([authorId, id])
            }
        """, "prisma/schema.prisma")
        u, p = r["tables"]["users"], r["tables"]["Post"]
        self.assertEqual(u["pk"], ["id"])
        self.assertEqual(col(u, "role")["values"], ["USER", "ADMIN"])
        self.assertEqual(col(u, "id")["default"], "autoincrement")
        self.assertNotIn("posts", [c["name"] for c in u["columns"]])
        self.assertEqual(p["fks"], [{"columns": ["author_id"], "to": "users", "to_columns": ["id"],
                                     "on_delete": "CASCADE"}])
        self.assertTrue(col(p, "meta")["null"])
        self.assertIn({"columns": ["author_id"], "unique": False}, p["indexes"])
        self.assertIn({"columns": ["author_id", "id"], "unique": True}, p["indexes"])
        self.assertEqual(r["maps"]["users"]["class"], "User")


class LiveTest(TempDir):
    def test_sqlite(self):
        db = self.root / "app.db"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                                kind TEXT CHECK (kind IN ('a', 'b')));
            CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE, body TEXT DEFAULT '');
            CREATE INDEX posts_user ON posts(user_id);
        """)
        con.close()
        r = live.sqlite(db)
        u, p = r["tables"]["users"], r["tables"]["posts"]
        self.assertEqual(u["pk"], ["id"])
        self.assertFalse(col(u, "email")["null"])
        self.assertEqual(col(u, "kind")["values"], ["a", "b"])
        self.assertIn({"columns": ["email"], "unique": True}, u["indexes"])
        self.assertEqual(p["fks"], [{"columns": ["user_id"], "to": "users", "to_columns": ["id"],
                                     "on_delete": "CASCADE"}])
        self.assertIn({"columns": ["user_id"], "unique": False}, p["indexes"])
        with self.assertRaises(live.LiveError):
            live.sqlite(self.root / "nope.db")

    def test_postgres_url_stays_out_of_argv(self):
        env = live.pg_env("postgresql://bob:s%40cret@db.example:6543/shop?sslmode=require")
        self.assertEqual(env, {"PGHOST": "db.example", "PGPORT": "6543", "PGUSER": "bob", "PGPASSWORD": "s@cret",
                               "PGDATABASE": "shop", "PGSSLMODE": "require"})
        with self.assertRaises(live.LiveError) as e:
            os.environ.pop("ARCHPLAN_TEST_NO_URL", None)
            live.postgres("ARCHPLAN_TEST_NO_URL")
        self.assertIn("not set", str(e.exception))

    def test_postgres_catalog(self):
        r = live.from_catalog({"enums": {"mood": ["ok", "bad"]}, "tables": [
            {"name": "users", "comment": "people", "columns": [
                {"name": "id", "type": "bigint", "null": False, "default": "nextval('users_id_seq'::regclass)"},
                {"name": "mood", "type": "mood", "null": True, "default": None}],
             "constraints": ["PRIMARY KEY (id)", "CHECK ((id > 0))"],
             "indexes": ["CREATE UNIQUE INDEX users_mood ON public.users USING btree (mood)"]},
            {"name": "posts", "columns": [{"name": "user_id", "type": "bigint", "null": False}],
             "constraints": ["FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"], "indexes": None}]})
        u = r["tables"]["users"]
        self.assertEqual(u["pk"], ["id"])
        self.assertEqual(col(u, "mood")["values"], ["ok", "bad"])
        self.assertEqual(u["comment"], "people")
        self.assertIn({"columns": ["mood"], "unique": True}, u["indexes"])
        self.assertEqual(r["tables"]["posts"]["fks"][0]["on_delete"], "CASCADE")


class AccessTest(TempDir):
    def test_orm_write_and_sql_access(self):
        write(self.root, {
            "manage.py": "",
            "shop/__init__.py": "",
            "shop/models.py": """\
                from django.db import models
                class Order(models.Model):
                    total = models.IntegerField()
            """,
            "billing/__init__.py": "",
            "billing/pay.py": """\
                from shop.models import Order
                def pay():
                    Order.objects.create(total=1)
            """,
            "reports/__init__.py": "",
            "reports/sql.py": """\
                def q(cur):
                    cur.execute("SELECT * FROM shop_order JOIN shop_order o2 ON 1=1")
            """,
            "reports/views.py": "from shop.models import Order\nOrder.objects.filter(total=1)\n",
            "audit/__init__.py": "",
            "audit/log.py": "def log(cur):\n    cur.execute('INSERT INTO audit_log (x) VALUES (1)')\n",
            "schema.sql": "CREATE TABLE audit_log (x int);\n",
        })
        model = {
            "components": {c: {"name": c, "path": c, "match": [f"{c}/**"]} for c in ("shop", "billing", "reports",
                                                                                     "audit")},
            "types": {"shop.models.Order": {"name": "Order", "component": "shop", "file": "shop/models.py"}},
            "files": {
                "shop/models.py": {"component": "shop", "refs": []},
                "billing/pay.py": {"component": "billing", "refs": [{"to": "shop/models.py", "line": 1}]},
                "reports/sql.py": {"component": "reports", "refs": []},
                "reports/views.py": {"component": "reports", "refs": [{"to": "shop/models.py", "line": 1}]},
                "audit/log.py": {"component": "audit", "refs": []},
            }}
        sch = schema.build(self.root, model)
        self.assertEqual([s["kind"] for s in sch["sources"]], ["django", "sql"])
        self.assertEqual(sch["maps"]["shop_order"], {"type": "shop.models.Order", "component": "shop",
                                                     "via": "type"})
        self.assertEqual(sch["maps"]["audit_log"], {"component": "audit", "via": "writes"})
        acc = {(a["component"], a["table"], a["mode"]): a for a in sch["access"]}
        self.assertEqual(acc[("billing", "shop_order", "write")]["evidence"], ["billing/pay.py:3"])
        self.assertIn(("billing", "shop_order", "orm"), acc)
        self.assertIn(("reports", "shop_order", "orm"), acc)
        self.assertNotIn(("reports", "shop_order", "write"), acc)
        self.assertEqual(acc[("reports", "shop_order", "read")]["weight"], 1)
        self.assertEqual(acc[("audit", "audit_log", "write")]["evidence"], ["audit/log.py:2"])
        self.assertEqual(access.component_of(model, "billing/new.py"), "billing")

    def test_elixir_writes_need_the_schema_name(self):
        mp = {"module": "Shop.Orders.Order", "file": "lib/order.ex"}
        self.assertIsNotNone(access.write_at("elixir", "x |> Orders.Order.changeset(a) |> Repo.insert()", mp, False))
        self.assertIsNotNone(access.write_at("elixir", "alias Shop.Orders.Order\n%Order{} |> Repo.insert()", mp,
                                             False))
        self.assertIsNone(access.write_at("elixir", "Repo.insert(other)\n%Orders.Order{} = o", mp, False))
        self.assertIsNone(access.write_at("elixir", "Repo.insert(Order.changeset(x))", mp, True))


class OwnersTest(TempDir):
    """Owners from config.json schema.owners and from CREATE TABLE statements,
    for a Go project whose DDL is in string literals and migrations."""

    def setUp(self):
        super().setUp()
        write(self.root, {
            "internal/sqlitedb/schema.go": '''\
                package sqlitedb

                const schema = `
                CREATE TABLE IF NOT EXISTS "entities" (id TEXT PRIMARY KEY);
                CREATE TABLE comments (id TEXT PRIMARY KEY);
                `
            ''',
            "internal/sqlitedb/schema_test.go": 'package sqlitedb\n\nconst q = "CREATE TABLE relations (id int)"\n',
            "internal/sqlitecomments/db.go": 'package sqlitecomments\n\n'
                                             'var ddl = "create table if not exists comment_threads (id text)"\n',
            "internal/store/pgstore/migrations/0001_init.sql": "CREATE TABLE public.entities (id text);\n"
                                                               "CREATE TABLE relations (id text);\n",
            "internal/store/store.go": "package store\n",
        })
        db = self.root / "rela.db"
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE entities (id TEXT PRIMARY KEY); CREATE TABLE comments (id TEXT);"
                          "CREATE TABLE comment_threads (id TEXT); CREATE TABLE relations (id TEXT);"
                          "CREATE TABLE tags (id TEXT);")
        con.close()
        self.live = {"kind": "sqlite", **live.sqlite(db)}
        comps = ("sqlitedb", "sqlitecomments", "store", "store/pgstore")
        self.model = {
            "components": {c.split("/")[-1]: {"name": c, "path": f"internal/{c}"} for c in comps},
            "files": {f"internal/{c}/{f}": {"component": c.split("/")[-1], "refs": []}
                      for c, f in (("sqlitedb", "schema.go"), ("sqlitedb", "schema_test.go"),
                                   ("sqlitecomments", "db.go"), ("store", "store.go"))}}

    def owners(self, sch):
        return {t: (mp["component"], mp["via"]) for t, mp in sch["maps"].items()}

    def test_create_table(self):
        sch = schema.build(self.root, self.model, sources=[], live=self.live)
        # entities: sqlitedb and pgstore both create it. relations: the test file does not count.
        self.assertEqual(self.owners(sch), {"comments": ("sqlitedb", "create"),
                                            "comment_threads": ("sqlitecomments", "create"),
                                            "relations": ("pgstore", "create")})
        self.assertNotIn("warnings", sch)

    def test_config_wins(self):
        cfg = {"entities": "store", "comment*": "sqlitecomments", "nosuch": "store", "tags": "ghost"}
        sch = schema.build(self.root, self.model, sources=[], live=self.live, owners=cfg)
        self.assertEqual(self.owners(sch), {"entities": ("store", "config"),
                                            "comments": ("sqlitecomments", "config"),
                                            "comment_threads": ("sqlitecomments", "config"),
                                            "relations": ("pgstore", "create")})
        self.assertEqual(sch["warnings"], ["schema owners: no component 'ghost' in the model",
                                           "schema owners: 'nosuch' match no table"])
        # An exact name wins over a glob, whatever their order.
        sch = schema.build(self.root, self.model, sources=[], live=self.live,
                           owners={"comment*": "store", "comments": "sqlitedb"})
        self.assertEqual(self.owners(sch)["comments"], ("sqlitedb", "config"))
        self.assertEqual(self.owners(sch)["comment_threads"], ("store", "config"))
        sch = schema.build(self.root, self.model, sources=[], live=self.live, owners=["entities"])
        self.assertEqual(len(sch["warnings"]), 1)

    def test_enrich_reads_the_config(self):
        from types import SimpleNamespace
        (self.root / ".archplan").mkdir()
        mp = self.root / ".archplan/model.json"
        mp.write_text(json.dumps(self.model))
        p = SimpleNamespace(root=self.root, model_path=mp, path=lambda n: self.root / ".archplan" / n,
                            config={"schema": {"owners": {"entities": "store"}}})
        sch = schema.enrich(p, live=self.live, force=True)
        self.assertEqual(sch["maps"]["entities"], {"component": "store", "via": "config"})
        self.assertEqual(json.loads(mp.read_text())["schema"]["maps"]["entities"]["via"], "config")


class Ctx:
    """The parts of the detector context the data detectors use."""

    def __init__(self, model, layers=None):
        self.model = model
        self._layers = layers

    @property
    def profile(self):
        if self._layers is None:
            raise ValueError("no profile")
        return {"layers": self._layers}, {}

    def fid(self, kind, *keys):
        return fingerprint(kind, *keys)


def tbl(*cols, fks=(), pk=("id",)):
    columns = [{"name": "id", "type": "bigint", "null": False}] + [
        {"name": c[0], "type": c[1], "null": c[2] if len(c) > 2 else True, **({"values": c[3]} if len(c) > 3 else {})}
        for c in cols]
    return {"columns": columns, "pk": list(pk), "fks": list(fks), "indexes": [], "checks": []}


def acc(comp, table, mode, weight=1):
    return {"component": comp, "table": table, "mode": mode, "weight": weight, "evidence": [f"{comp}/x.py:1"]}


def detector_model():
    return {
        "components": {c: {"name": c} for c in ("web", "orders", "billing", "users", "ops")},
        "dependencies": [{"from": "orders", "to": "users"}],
        "enums": [{"name": "Order.state", "values": ["draft", "paid", "sent"]}],
        "literals": [{"value": "refunded", "component": "billing", "field": "status", "context": "compare"},
                     {"value": "open", "component": "billing", "field": "status", "context": "compare"}],
        "schema": {
            "tables": {
                "orders": tbl(("user_id", "bigint"), ("state", "varchar", False, ["draft", "paid"]),
                              ("meta", "jsonb"), ("street", "text"), ("city", "text"), ("zip", "text"),
                              fks=[{"columns": ["user_id"], "to": "users", "to_columns": ["id"],
                                    "on_delete": "CASCADE"}]),
                "users": tbl(("street", "text"), ("city", "text"), ("zip", "text"), ("is_admin", "boolean"),
                             ("banned_at", "timestamp"), ("verified_at", "timestamptz"),
                             fks=[{"columns": ["id"], "to": "invoices", "to_columns": ["id"]}]),
                "invoices": tbl(("street", "text"), ("city", "text"), ("zip", "text"),
                                ("status", "varchar", False, ["open", "paid"]),
                                ("subject_type", "varchar"), ("subject_id", "bigint")),
                "legacy": tbl(("x", "int")),
            },
            "maps": {"orders": {"type": "Order", "component": "orders"}, "users": {"component": "users"},
                     "invoices": {"component": "billing"}},
            "access": [acc("orders", "orders", "write", 3), acc("billing", "orders", "write"),
                       acc("web", "orders", "orm"), acc("billing", "orders", "orm"),
                       acc("users", "users", "write"), acc("billing", "invoices", "write"),
                       acc("billing", "invoices", "orm", 2)],
        }}


class DetectorTest(unittest.TestCase):
    def run_det(self, mod, layers=None, **params):
        return mod.run(Ctx(detector_model(), layers), {**mod.PARAMS, **params})

    def test_shared_table(self):
        fs = self.run_det(data_shared_table)
        self.assertEqual([f["evidence"]["table"] for f in fs], ["orders"])
        self.assertEqual(fs[0]["components"], ["orders", "billing"])
        self.assertEqual(fs[0]["kind"], "data.shared_table")

    def test_cross_fk_with_layers(self):
        fs = self.run_det(data_cross_fk)
        # orders → users follows a code dependency, so it shows only on request.
        self.assertEqual({(f["evidence"]["from"], f["evidence"]["to"]) for f in fs}, {("users", "billing")})
        fs = self.run_det(data_cross_fk, include_coded=True)
        self.assertEqual({(f["evidence"]["from"], f["evidence"]["to"]) for f in fs},
                         {("orders", "users"), ("users", "billing")})
        layers = [{"name": "Web", "components": ["web"]}, {"name": "Domain", "components": ["orders", "billing"]},
                  {"name": "Base", "components": ["users"]}]
        by = {(f["evidence"]["from"], f["evidence"]["to"]): f for f in self.run_det(data_cross_fk, layers, include_coded=True)}
        self.assertEqual(by[("users", "billing")]["evidence"]["direction"], "up")
        self.assertEqual(by[("users", "billing")]["strength"], "strong")
        self.assertEqual(by[("orders", "users")]["evidence"]["direction"], "down")

    def test_polymorphic(self):
        fs = self.run_det(data_polymorphic)
        self.assertEqual([(f["evidence"]["table"], f["evidence"]["pairs"]) for f in fs],
                         [("invoices", [{"type": "subject_type", "id": "subject_id"}])])

    def test_json_bag(self):
        fs = self.run_det(data_json_bag)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["evidence"]["components"], ["billing", "orders", "web"])
        self.assertEqual(fs[0]["strength"], "strong")

    def test_flag_columns(self):
        fs = self.run_det(data_flag_columns)
        self.assertEqual([f["evidence"]["table"] for f in fs], ["users"])
        self.assertEqual(fs[0]["evidence"]["booleans"], ["is_admin"])

    def test_repeated_columns(self):
        fs = self.run_det(data_repeated_columns)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["evidence"]["columns"], ["city", "street", "zip"])
        self.assertEqual(fs[0]["evidence"]["tables"], ["invoices", "orders", "users"])
        self.assertEqual(self.run_det(data_repeated_columns, min_tables=4), [])

    def test_enum_mismatch(self):
        fs = {f["evidence"]["table"]: f for f in self.run_det(data_enum_mismatch)}
        self.assertEqual(fs["orders"]["evidence"]["only_code"], ["sent"])
        self.assertEqual(fs["orders"]["evidence"]["code_enum"], "Order.state")
        self.assertNotIn("invoices", fs)  # literals only on request
        fs = {f["evidence"]["table"]: f for f in self.run_det(data_enum_mismatch, use_literals=True)}
        self.assertEqual(fs["invoices"]["evidence"]["only_code"], ["refunded"])
        self.assertEqual(fs["invoices"]["evidence"]["source"], "literals")

    def test_orphan_table(self):
        fs = self.run_det(data_orphan_table)
        self.assertEqual([f["evidence"]["table"] for f in fs], ["legacy"])
        self.assertEqual(fs[0]["strength"], "medium")

    def test_focus(self):
        m = detector_model()
        m["types"] = {"Order": {"name": "Order", "component": "orders",
                                "fields": [{"name": "user", "ref": "User"}, {"name": "meta", "type": "map"},
                                           {"name": "street", "type": "string"}]}}
        run = lambda mod, **kw: mod.run(Ctx(m), {**mod.PARAMS, **kw})
        (shared,) = run(data_shared_table)
        self.assertEqual([(e["view"], e["nodes"]) for e in shared["focus"]],
                         [("types", ["Order"]), ("components", ["orders", "billing"])])
        (bag,) = run(data_json_bag)
        self.assertEqual(bag["focus"][0]["fields"], {"Order": ["meta"]})
        fk = {(f["evidence"]["from"], f["evidence"]["to"]): f for f in run(data_cross_fk, include_coded=True)}
        fc = fk[("orders", "users")]["focus"]
        self.assertEqual((fc[0]["view"], fc[0]["fields"]), ("types", {"Order": ["user"]}))  # user_id is the user field
        self.assertEqual((fc[1]["view"], fc[1]["edges"]), ("components", [["orders", "users"]]))
        (orphan,) = run(data_orphan_table)
        self.assertNotIn("focus", orphan)  # and no owner either: nothing to show
        flags = run(data_flag_columns)[0]["focus"]
        self.assertEqual([(e["view"], e["nodes"]) for e in flags], [("components", ["users"])])
        for f in run(data_enum_mismatch) + run(data_polymorphic) + run(data_repeated_columns):
            self.assertTrue(f["focus"], f["kind"])

    def test_registered(self):
        from archplan import detectors
        ids = {d for d in detectors.builtins() if d.startswith("data.")}
        self.assertEqual(ids, {"data.shared_table", "data.cross_fk", "data.polymorphic", "data.json_bag",
                               "data.flag_columns", "data.repeated_columns", "data.enum_mismatch",
                               "data.orphan_table"})
        for d in ids:
            m = detectors.builtins()[d]
            self.assertEqual((m.GROUP, m.NEEDS), ("data", {"schema"}))


class ProjectTest(TempDir):
    def test_export_adds_schema_and_cli(self):
        write(self.root, {
            "manage.py": "",
            "shop/__init__.py": "",
            "shop/models.py": "from django.db import models\nclass Order(models.Model):\n"
                              "    total = models.IntegerField()\n",
        })
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with redirect_stdout(StringIO()):
                cli.main(["init", "--export", "archplan export-python -o .archplan/model.archmodel.json"])
                cli.main(["export"])
            m = json.loads((self.root / ".archplan/model.archmodel.json").read_text())
            self.assertIn("schema", m["source"]["capabilities"])
            self.assertEqual(m["schema"]["maps"]["shop_order"]["type"], "shop.models.Order")
            out = StringIO()
            with redirect_stdout(out):
                cli.main(["schema", "--json"])
            self.assertIn("shop_order", json.loads(out.getvalue())["tables"])
            db = self.root / "extra.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
            con.close()
            out = StringIO()
            with redirect_stdout(out):
                cli.main(["schema", "--sqlite", str(db)])
            self.assertIn("sqlite extra.db", out.getvalue())
            live_file = json.loads((self.root / ".archplan/schema-live.json").read_text())
            self.assertEqual(live_file["path"], "extra.db")
            with redirect_stdout(StringIO()):
                cli.main(["export"])  # the live result survives a new export
            m = json.loads((self.root / ".archplan/model.archmodel.json").read_text())
            self.assertIn("extra", m["schema"]["tables"])
        finally:
            os.chdir(cwd)


MB = 1 << 20


def mb(unit: str) -> str:
    return (unit * (MB // len(unit) + 1))[:MB]


class HostileInputTest(TempDir):
    """1 MB inputs shaped to make a backtracking regex or a per-match rescan
    quadratic. A quadratic reader takes minutes on these; a linear one takes
    well under a second on most, and one or two seconds on inputs with a token
    every one or two characters. CPU time is measured, so a busy machine does
    not fail the test."""

    LIMIT = 4.0

    def fast(self, label, fn, *args):
        import time
        t = time.process_time()
        fn(*args)
        dt = time.process_time() - t
        self.assertLess(dt, self.LIMIT, f"{label} took {dt:.2f}s")

    def test_ecto(self):
        from archplan.schema import exterm
        self.fast("defmodule after blank lines", ecto.modules_at, mb("\n") + "defmodule A do\nend\n")
        self.fast("many defmodules", ecto.modules_at, mb("defmodule A do\n"))
        for name, text in {"blank lines": "schema \n" + mb("\n"), "spaces": "schema " + mb(" \n"),
                           "many schemas": mb('schema "t" do\n'), "unclosed alias": mb("alias A.{\n"),
                           "unclosed pk": "use Ecto.Schema\n" + mb("@primary_key {\n")}.items():
            (self.root / "lib").mkdir(exist_ok=True)
            (self.root / "lib/a.ex").write_text(text)
            self.fast(name, ecto.read_schemas, self.root, ["lib/a.ex"])
        for unit in (" \n", '"\\', "~w(", "/*"):
            self.fast(f"statements {unit!r}", lambda: list(exterm.statements(mb(unit).split("\n"))))
        self.fast("tokens", exterm.tokens, mb('"\\'))

    def test_prisma(self):
        self.fast("unclosed blocks", prisma.read, mb("model A {\n"))
        self.fast("unclosed block, blank lines", prisma.read, "model A {\n" + mb(" \n"))
        self.fast("many unclosed attributes", prisma.read, "model A {\n" + mb("  a Int @@index([\n") + "}\n")
        self.fast("unclosed brackets in one line", prisma.read, "model A {\n  a Int @@index(" + mb("[") + "\n}\n")
        r = prisma.read("model A {\n  id Int @id\n  @@index([id])\n}\nenum E {\n  x\n}\n")
        self.assertEqual(r["tables"]["A"]["indexes"], [{"columns": ["id"], "unique": False}])

    def test_ddl_rails(self):
        for unit in ("/*a", "'a\\", "[a", "$a$", "COMMENT '"):
            self.fast(f"ddl {unit!r}", ddl.parse, "CREATE TABLE a (" + mb(unit) + ");")
        (self.root / "app/models").mkdir(parents=True)
        (self.root / "app/models/a.rb").write_text(mb("class A < ApplicationRecord\n"))
        s = {"tables": {"as": {"columns": []}}, "enums": {}, "maps": {}}
        self.fast("rails models", rails.models, self.root, s)
        (self.root / "db").mkdir()
        (self.root / "db/schema.rb").write_text('create_table "a" do |t|\n' + mb(" \n") + "end\n")
        self.fast("rails schema", rails.read, self.root, self.root / "db/schema.rb")

    def test_lite_vocab_fields(self):
        from archplan import liteexport, vocab
        from archplan.detectors import fields
        for unit in ('"\\', "/*a", "@Entity\n", "[package]\n", "public\n", "  private a = a ", "x" * 3000 + "\n",
                     "use a\\\n", "import a.{\n", "class A\n"):
            for lang in ("java", "csharp", "php", "ruby", "kotlin", "rust", "scala", "swift"):
                self.fast(f"lite {lang} {unit[:12]!r}", liteexport.parse, liteexport.Src("A.x", lang, mb(unit)))
        for unit in ("~w(", '"\\', "/*a", "=begin\n", "`\\"):
            for lang in ("elixir", "js", "ruby", "python"):
                self.fast(f"vocab {lang} {unit!r}", vocab.scan_text, mb(unit) if unit != "~w(" else mb("~w("), lang)
        self.fast("nil type", fields.type_text, {"type": mb(" ")})
        self.assertEqual(fields.type_text({"type": "String | nil"}), "string")

    def test_access(self):
        ex, py = {"module": "App.Accounts.User"}, {"class": "User"}
        self.fast("unclosed alias groups", access.write_at, "elixir", "Repo.insert(\n" + mb("alias App.Accounts.{\n"), ex, False)
        self.fast("model = without save", access.write_at, "python", "x.save()\n" + mb("model = User\n"), py, False)
        self.assertIn("User", access.forms("alias App.Accounts.{Org,\n User}\n", ex))
        self.assertEqual(access.write_at("python", "model = User\nx.save()\n", py, False), 0)
        self.assertIsNone(access.write_at("python", "x.save()\nmodel = User\n", py, False))

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_ts_script_blocks(self):
        import subprocess
        src = (ROOT / "src/archplan/ts/archplan_export.mjs").read_text()
        body = src[src.index("const sfcCache"):src.index("// ---------- the program")]
        js = self.root / "sfc.cjs"
        js.write_text("const fs = require('fs');\n" + body + "\nmodule.exports = sfc;\n")
        (self.root / "a.vue").write_text('<template>x</template>\n<script lang="ts">\nlet a = 1\n</script>\n')
        (self.root / "h.vue").write_text(mb("<script>"))
        out = subprocess.run(["node", "-e", f"""
            const sfc = require({json.dumps(str(js))});
            const a = sfc({json.dumps(str(self.root / "a.vue"))});
            const t = Date.now(); sfc({json.dumps(str(self.root / "h.vue"))});
            console.log(JSON.stringify({{text: a.text, ms: Date.now() - t}}));"""],
            capture_output=True, text=True, timeout=30)
        r = json.loads(out.stdout)
        self.assertIn("let a = 1", r["text"])
        self.assertNotIn("template", r["text"])
        self.assertLess(r["ms"], 500)


class LiveReadOnlyTest(TempDir):
    def test_sqlite_name_cannot_change_the_mode(self):
        name = "app.db?mode=rwc&cache=shared"
        db = self.root / name
        con = sqlite3.connect(str(db))  # a plain path: the ? is part of the file name
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        r = live.sqlite(db)
        self.assertIn("users", r["tables"])
        self.assertFalse((self.root / "app.db").exists())  # the old URI opened app.db read-write
        uri = live.sqlite_uri(db)
        self.assertTrue(uri.endswith("?mode=ro") and "%3F" in uri)
        con = sqlite3.connect(uri, uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("CREATE TABLE x (a)")
        con.close()



class SubfolderTest(TempDir):
    """A project in a subfolder of its git repository: the exporter writes
    paths relative to the repository root (core/lib/...), the schema scan
    reads files under the project root (lib/...)."""

    def setUp(self):
        super().setUp()
        (self.root / ".git").mkdir()
        self.core = self.root / "core"
        write(self.core, {
            "mix.exs": "defp deps, do: [{:ecto_sql, \"~> 3.0\"}]\n",
            "lib/core/authentication/actor.ex": """\
                defmodule Core.Authentication.Actor do
                  use Ecto.Schema
                  schema "actor" do
                    field :name, :string
                  end
                end
            """,
            "lib/core/authentication/legacy.ex": """\
                defmodule Core.Authentication.Legacy do
                  use Ecto.Schema
                  schema "legacy" do
                    field :name, :string
                  end
                end
            """,
            "lib/core/authentication.ex": """\
                defmodule Core.Authentication do
                  alias Core.Repo
                  alias Core.Authentication

                  def obtain_actor(name) do
                    Repo.one(Authentication.Actor, name: name)
                  end
                end
            """,
        })
        auth = "core/lib/core/authentication"
        self.model = {
            "components": {"Core.Authentication": {"name": "Core.Authentication", "path": auth,
                                                   "match": [f"{auth}.ex", f"{auth}/**"]}},
            "types": {f"Core.Authentication.{n}": {"name": n, "component": "Core.Authentication",
                                                   "file": f"{auth}/{n.lower()}.ex"} for n in ("Actor", "Legacy")},
            "files": {f"{auth}.ex": {"component": "Core.Authentication",
                                     "refs": [{"to": f"{auth}/actor.ex", "line": 5}]},
                      f"{auth}/actor.ex": {"component": "Core.Authentication", "refs": []},
                      f"{auth}/legacy.ex": {"component": "Core.Authentication", "refs": []}},
        }

    def test_access_through_alias(self):
        sch = schema.build(self.core, self.model)
        uses = {(a["table"], a["mode"]): a for a in sch["access"]}
        self.assertEqual(uses[("actor", "orm")]["weight"], 2)  # its own file and authentication.ex
        self.assertIn("core/lib/core/authentication.ex:5", uses[("actor", "orm")]["evidence"])
        model = {**self.model, "schema": sch}
        fs = data_orphan_table.run(Ctx(model), data_orphan_table.PARAMS)
        self.assertEqual([f["evidence"]["table"] for f in fs], ["legacy"])


if __name__ == "__main__":
    unittest.main()
