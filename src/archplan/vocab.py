"""The domain vocabulary of the code: candidate glossary terms per component,
and the vocabulary section for exporters that do not write one.

Each source file is read in three channels: identifiers, string literals and
comments. Identifiers split into words (camelCase, snake_case, Module.Names);
words are lowercased and plurals folded. Bigrams are adjacent words inside one
identifier ("payout account").

A word's score in a component is tf-idf: tf = log(1 + identifier count + half
the string and comment count), idf = log(N / df) over components. Words in the
component's type and module names get a boost. Words in more than half of the
components, and built-in stopwords, drop out.

Per-file counts are cached in .archplan/vocab-cache.json (not committed) by
file size and modification time. Terms to leave out live in the committed
.archplan/vocabulary.json: {"ignore": [terms], "ignore_in": {component: [terms]}}.

After an export without a vocabulary section, enrich() adds one: concepts
from GLOSSARY.md files (the same format the Go exporter reads), vendor
concepts for external components, the top candidates per component as
`suggested`, and `usage` where code names a concept it does not own."""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .architecture import source_files
from .hotspots import Mapper
from .project import Project, append_ignore, etag, read_json, write_json
from .safeio import inside, model_base, read_file

CACHE = "vocab-cache.json"
FILE = "vocabulary.json"
CACHE_VERSION = 3
GLOSSARY = "GLOSSARY.md"
MAX_FILE = 1_000_000      # bytes; larger files are skipped
MIN_LEN = 3               # shorter words are skipped
MAX_TERM_WORDS = 3        # a concept term matches up to this many consecutive words
SUGGESTED = 10            # per component, in the vocabulary section
EVIDENCE = 3
TYPE_BOOST = 1.5
TEXT_WEIGHT = 0.5         # strings and comments count half
PHRASE_SHARE = 0.6        # a word mostly used inside one phrase gives way to the phrase
TERM_MAX = 60             # characters of an ignored term

# ---------- words ----------

KEYWORDS = """
and as assert async await break class continue def del elif else except finally for from global if import in is
lambda nonlocal not or pass raise return try while with yield none true false self cls super print
defmodule defp defmacro defmacrop defstruct defprotocol defimpl defdelegate defguard defexception defoverridable
do end fn when case cond unless alias require use quote unquote receive after rescue catch nil import
func package type struct interface map chan go defer select switch fallthrough goto var const range string byte rune
int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 float32 float64 bool error make len cap append copy panic
function let new this typeof instanceof void delete export default extends implements static public private
protected readonly enum declare namespace module abstract async undefined null any unknown never number boolean
object symbol bigint keyof infer require begin ensure elsif then until redo retry yield attr accessor
""".split()

PROGRAMMING = """
get set put add remove delete update create insert upsert read write load save find fetch list map filter reduce sort
put has can should must may make build run start stop close open init new call apply handle process parse format
render show view index edit destroy validate valid check ensure assert expect test tests spec describe should mock
stub fixture factory setup teardown data value values key keys item items entry entries field fields attr attrs
attribute attributes param params arg args argv kwargs opts opt option options config conf settings setting env
id ids uuid pk name names type types kind kinds mode state status result results response request req resp res
error errors err exception ok fail failure success true false nil null none undefined default defaults
conn socket assigns assign session context ctx self cls this that other obj object instance class module modules
func function method methods callback handler handlers helper helpers util utils utility utilities lib libs
base core common shared generic main app application api client server service services manager managers
string str text char int integer float decimal number num bool boolean byte bytes array tuple dict hash list set
date time datetime timestamp naive utc local timezone day days hour hours minute minutes second seconds
count total size length len min max sum avg first last next prev previous current old new tmp temp
input output source target src dst from into with without before after when where while for each all any some
file files path paths dir dirs url urls uri link links http https json xml yaml html csv
log logs logger debug info warn warning trace message messages msg event events signal signals notify
query queries repo repository schema schemas model models migration migrations table tables column columns row rows
changeset changesets cast validate_required put_change get_field preload preloads repo insert_all
live live_view liveview component components template templates layout layouts html heex slot slots page pages
form forms button buttons modal modals icon icons label labels title titles text_field class_name style css
view views controller controllers router routes route plug plugs pipeline endpoint socket channel channels
serializer serializers viewset viewsets admin permission permissions queryset querysets meta django drf
the and for not but are was were been being have has had does did doing done will would could this these those
its our your their his her him she they them you who what which why how than then there here also only just very
such into onto over under above below between within about across again further once same own too more most less
one two three four five six seven eight nine ten zero
use used uses using via per etc todo fixme note see
uid since until cascade protect verbose blank related choices help lazy gettext translation translate
enabled disabled active inactive empty exists missing found allow allowed unique required optional
""".split()

DUTCH = """
de het een van voor met niet wordt zijn naar deze die dat bij ook als aan uit door over kan moet worden geen alle
welke waar indien dan nog wel maar tot hun zij wij onder heeft hebben mag mogen zal zullen dit wat wie
""".split()
STOP = frozenset(KEYWORDS) | frozenset(PROGRAMMING) | frozenset(DUTCH)
KEEP_S = ("ss", "us", "is", "os", "as", "ics")
KEEP = frozenset("""
status bus process access address analysis basis series species news canvas alias class business
""".split())


def fold(w: str) -> str:
    """A rough singular: entities -> entity, batches -> batch, carts -> cart,
    status -> status."""
    if w in KEEP or len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "ches", "shes", "xes", "zzes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(KEEP_S):
        return w[:-1]
    return w


CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split(name: str) -> list:
    """Lowercase words of an identifier: parseHTTPRequest_v2 -> parse, http, request, v, 2."""
    return [w.lower() for w in CAMEL.findall(name)]


def words(name: str) -> list:
    """Folded words of an identifier or phrase, numbers left out."""
    return [fold(w) for w in split(name) if not w.isdigit()]


def term_key(term: str) -> str:
    """'Cart item', 'cartItems' and 'CART_ITEM' all become 'cart item'."""
    return " ".join(words(term))


def keep(w: str) -> bool:
    return len(w) >= MIN_LEN and w not in STOP and not w.isdigit()


# ---------- channels ----------

# An unclosed string, comment or sigil runs to the end of its line or of the
# text. Without that, the scanner would scan on from every opening quote:
# quadratic on hostile input.
_END = r"\\?(?=\n)|\\?\Z"  # a lone backslash before the end closes too
_STR_DQ = r'"(?:[^"\\\n]|\\.)*(?:"|' + _END + ")"
_STR_SQ = r"'(?:[^'\\\n]|\\.)*(?:'|" + _END + ")"
_TRIPLE = r'"""[\s\S]*?(?:"""|\Z)|\'\'\'[\s\S]*?(?:\'\'\'|\Z)'
_BLOCK = r"/\*[\s\S]*?(?:\*/|\Z)"
LANGS = {
    "python": (r"#[^\n]*", "|".join([_TRIPLE, _STR_DQ, _STR_SQ])),
    "elixir": (r"#[^\n]*", "|".join([r'"""[\s\S]*?(?:"""|\Z)', _STR_DQ, r"~[a-zA-Z]\([^)\n]*(?:\)|" + _END + ")",
                                    r"~[a-zA-Z]\[[^\]\n]*(?:\]|" + _END + ")"])),
    "go": (r"//[^\n]*|" + _BLOCK, "|".join([r"`[^`]*(?:`|\Z)", _STR_DQ, _STR_SQ])),
    "js": (r"//[^\n]*|" + _BLOCK, "|".join([r"`(?:[^`\\]|\\.)*(?:`|\\?\Z)", _STR_DQ, _STR_SQ])),
    "ruby": (r"#[^\n]*|^=begin[\s\S]*?(?:^=end|\Z)", "|".join([_STR_DQ, _STR_SQ])),
    "generic": (r"#[^\n]*|//[^\n]*|" + _BLOCK, "|".join([_STR_DQ, _STR_SQ])),
}
EXT = {".py": "python", ".ex": "elixir", ".exs": "elixir", ".heex": "elixir", ".go": "go", ".js": "js", ".jsx": "js",
       ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js", ".mts": "js", ".cts": "js", ".vue": "js",
       ".svelte": "js", ".rb": "ruby"}
SCANNERS = {lang: re.compile(f"(?P<c>{c})|(?P<s>{s})", re.M) for lang, (c, s) in LANGS.items()}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WORDISH = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def channels(text: str, lang: str):
    """Yield (channel, offset, chunk): 0 identifiers, 1 strings, 2 comments.
    A chunk is one identifier, or one string or comment."""
    pos = 0
    for m in SCANNERS.get(lang, SCANNERS["generic"]).finditer(text):
        if m.start() > pos:
            for x in IDENT.finditer(text, pos, m.start()):
                yield 0, x.start(), x.group()
        yield (2 if m.group("c") is not None else 1), m.start(), m.group()
        pos = m.end()
    for x in IDENT.finditer(text, pos):
        yield 0, x.start(), x.group()


def raw_words(name: str) -> list:
    """Lowercase words of an identifier or phrase, unfolded, numbers left out."""
    return [w for w in split(name) if not w.isdigit()]


def keep_raw(w: str) -> bool:
    return keep(w) and keep(fold(w))


def scan_text(text: str, lang: str) -> dict:
    """Counts for one file, by unfolded word: {"w": {word: [ident, string,
    comment]}, "b": {bigram: [...]}, "x": {word: [identifiers]}, "l": {word:
    first line}}. Plurals are folded later, against the whole corpus."""
    nl = [i for i, ch in enumerate(text) if ch == "\n"]
    w, b, x, ln = {}, {}, {}, {}
    for ch, off, chunk in channels(text, lang):
        for piece in ([chunk] if ch == 0 else WORDISH.findall(chunk)):
            all_w = raw_words(piece)
            ws = [v for v in all_w if keep_raw(v)]
            if ch == 0:
                for a, c in zip(all_w, all_w[1:]):
                    if a != c and keep_raw(a) and keep_raw(c):
                        b.setdefault(a + " " + c, [0, 0, 0])[0] += 1
            for v in ws:
                cnt = w.get(v)
                if cnt is None:
                    cnt = w[v] = [0, 0, 0]
                    ln[v] = bisect.bisect_left(nl, off) + 1
                cnt[ch] += 1
                if ch == 0:
                    xs = x.setdefault(v, [])
                    if len(xs) < 3 and piece not in xs:
                        xs.append(piece)
    return {"w": w, "b": b, "x": x, "l": ln}


def fold_map(corpus) -> dict:
    """word -> its folded form when that form also occurs in the corpus, so
    'zaken' or 'adres' (not English plurals) stay as they are."""
    out = {}
    for v in corpus:
        f = fold(v)
        out[v] = f if f != v and f in corpus else v
    return out


def lang_of(path: str) -> str:
    return EXT.get(os.path.splitext(path)[1], "generic")


def is_test(path: str) -> bool:
    n = path.rsplit("/", 1)[-1]
    return (n.startswith("test_") or n.endswith(("_test.py", "_test.go", "_test.exs", ".test.ts", ".test.js",
                                                 ".test.tsx", ".test.jsx", ".spec.ts", ".spec.js", ".spec.tsx",
                                                 "_spec.rb", "conftest.py")))


# ---------- ignore list ----------

def clean_ignore(d) -> dict:
    """.archplan/vocabulary.json is committed repo data: keep only short strings."""
    d = d if isinstance(d, dict) else {}
    term = lambda t: term_key(str(t)[:TERM_MAX]) if isinstance(t, (str, int)) else ""
    ig = d.get("ignore") if isinstance(d.get("ignore"), list) else []
    ins = d.get("ignore_in") if isinstance(d.get("ignore_in"), dict) else {}
    return {"ignore": sorted({t for t in map(term, ig[:5000]) if t}),
            "ignore_in": {str(c)[:200]: sorted({t for t in map(term, v[:5000]) if t})
                          for c, v in list(ins.items())[:2000] if isinstance(v, list)}}


# ---------- the analysis ----------


class Vocab:
    def __init__(self, p: Project):
        self.p = p
        self.cache_path = p.path(CACHE)
        self.ignore_path = p.path(FILE)
        self._result = (None, None)

    # ignore list

    def ignored(self) -> dict:
        return clean_ignore(read_json(self.ignore_path))

    def set_ignored(self, term: str, component: str | None, on: bool) -> dict:
        t = term_key(str(term or "")[:TERM_MAX])
        if not t:
            raise ValueError("term is empty")
        d = self.ignored()
        if component:
            lst = set(d["ignore_in"].get(component, []))
            (lst.add if on else lst.discard)(t)
            if lst:
                d["ignore_in"][component] = sorted(lst)
            else:
                d["ignore_in"].pop(component, None)
        else:
            lst = set(d["ignore"])
            (lst.add if on else lst.discard)(t)
            d["ignore"] = sorted(lst)
        write_json(self.ignore_path, d)
        return d

    # per-file counts

    def base(self, model: dict) -> Path:
        """The folder the model's paths are relative to (see safeio.model_base)."""
        return model_base(self.p.root, model)

    def file_counts(self, files: list, base: Path | None = None) -> dict:
        """{path: counts} for the files, from the cache when unchanged. The
        paths are relative to base, the project root by default."""
        base = base or self.p.root
        old = read_json(self.cache_path) or {}
        if old.get("version") != CACHE_VERSION:
            old = {}
        entries = old.get("files") or {}
        out, dirty = {}, False
        for f in files:
            fp = inside(base, f)
            if fp is None:
                continue
            try:
                st = fp.lstat()
            except OSError:
                continue
            tag = f"{st.st_size:x}-{st.st_mtime_ns:x}"
            e = entries.get(f)
            if e and e.get("e") == tag:
                out[f] = e
                continue
            if st.st_size > MAX_FILE:
                continue
            try:
                text = read_file(fp, MAX_FILE)
            except OSError:
                continue
            out[f] = {"e": tag, **scan_text(text, lang_of(f))}
            dirty = True
        if dirty or set(entries) != set(out):
            write_json(self.cache_path, {"version": CACHE_VERSION, "files": out})
            self._gitignore()
        return out

    def _gitignore(self):
        append_ignore(self.p, CACHE)

    def files_by_component(self, model: dict) -> dict:
        mapper = Mapper(model.get("components") or {})
        by = defaultdict(list)
        for f in source_files(model, self.p.root):
            if is_test(f) or f.endswith(GLOSSARY):
                continue
            c = mapper(f)
            if c:
                by[c].append(f)
        return by

    def analyse(self, model: dict) -> dict:
        """Candidates per component: {component: [term, ...]} sorted by score,
        each term a dict as candidates() returns it; plus "n" and "took_ms"."""
        key = (id(model), etag(self.p.model_path))
        if self._result[0] == key[1:] and self._result[1] is not None:
            return self._result[1]
        t0 = time.monotonic()
        by = self.files_by_component(model)
        listed = sorted(f for fs in by.values() for f in fs)
        base = self.base(model)
        counts = self.file_counts(listed, base)
        comp_types = defaultdict(list)
        for tid, t in (model.get("types") or {}).items():
            if t.get("component"):
                comp_types[t["component"]].append(t.get("name") or tid.rsplit(".", 1)[-1])
        tf = {}        # component -> term -> [ident, string, comment]
        samples = {}   # component -> term -> identifiers
        sites = {}     # component -> term -> [file:line]
        corpus = {v for e in counts.values() for v in e["w"]}
        fm = fold_map(corpus)
        fb = lambda k: " ".join(fm.get(x, x) for x in k.split(" "))
        for c, fs in by.items():
            agg, smp, st = {}, defaultdict(list), defaultdict(list)
            for f in fs:
                e = counts.get(f)
                if not e:
                    continue
                for section, norm in (("w", fm.get), ("b", lambda k, _=None: fb(k))):
                    for raw, n in e[section].items():
                        term = norm(raw, raw)
                        a = agg.get(term)
                        if a is None:
                            agg[term] = list(n)
                        else:
                            a[0] += n[0]
                            a[1] += n[1]
                            a[2] += n[2]
                for raw, xs in e["x"].items():
                    s = smp[fm.get(raw, raw)]
                    if len(s) < 5:
                        s.extend(v for v in xs if v not in s and len(s) < 5)
                for raw, line in e["l"].items():
                    sl = st[fm.get(raw, raw)]
                    if len(sl) < EVIDENCE and not any(x.startswith(f + ":") for x in sl):
                        sl.append(f"{f}:{line}")
            tf[c], samples[c], sites[c] = agg, smp, st
        n = len(tf) or 1
        weight = lambda v: v[0] + TEXT_WEIGHT * (v[1] + v[2])
        df, best = Counter(), {}
        for c, agg in tf.items():
            for term, v in agg.items():
                if weight(v) >= 1:
                    df[term] += 1
                    b = best.get(term)
                    if b is None or (-weight(v), c) < (-b[0], b[1]):
                        best[term] = (weight(v), c)
        users = defaultdict(list)  # term -> [(weight, component)]
        for c, agg in tf.items():
            for term, v in agg.items():
                if df[term] > 1 and weight(v) >= 1:
                    users[term].append((weight(v), c))
        type_words = {}
        for c, names in comp_types.items():
            ws = set()
            for nm in names:
                parts = words(nm)
                ws.update(parts)
                ws.update(a + " " + b for a, b in zip(parts, parts[1:]))
            type_words[c] = ws
        comps = model.get("components") or {}
        for c in tf:
            info = comps.get(c) or {}
            mod = words(c) + words(info.get("name") or "")
            type_words.setdefault(c, set()).update(mod)
        # A type named after a word, such as Zaak, makes that word a core concept of its
        # component even when most components use it, which would otherwise drop it.
        own_types = {c: {" ".join(words(nm.rsplit(".", 1)[-1])) for nm in names} for c, names in comp_types.items()}
        for c in tf:  # the component's own name counts too, as in Systems.Assignment
            own_types.setdefault(c, set()).add(" ".join(words(c.rsplit(".", 1)[-1])))
        out = {}
        for c, agg in tf.items():
            rows = []
            for term, v in agg.items():
                if df[term] * 2 > n and n > 2:
                    if term in own_types.get(c, ()):
                        rows.append((math.log(1 + weight(v)) * math.log(n) * 0.5 * TYPE_BOOST, term, v))
                    continue
                if v[0] < 2 and weight(v) < 3:
                    continue
                if " " in term and v[0] < 3:
                    continue
                score = math.log(1 + weight(v)) * math.log(n / df[term] if n > 1 else 1.0) \
                    * (TYPE_BOOST if term in type_words.get(c, ()) else 1.0)
                if term in own_types.get(c, ()) and n > 2:  # a core concept scores at least like one used everywhere
                    score = max(score, math.log(1 + weight(v)) * math.log(n) * 0.5 * TYPE_BOOST)
                if n > 1 and score <= 0:
                    continue
                rows.append((score, term, v))
            rows.sort(key=lambda r: (-r[0], r[1]))
            # Prefer a phrase to its words: drop a word when one ranked bigram
            # holds most of its identifier uses (natuurlijk -> natuurlijk persoon).
            phrase = Counter()
            for _, term, v in rows:
                if " " in term:
                    for part in term.split(" "):
                        phrase[part] = max(phrase[part], v[0])
            rows = [r for r in rows if " " in r[1] or phrase[r[1]] < PHRASE_SHARE * r[2][0]]
            out[c] = [(round(s, 2), term, v) for s, term, v in rows[:200]]
        res = {"rank": out, "tf": tf, "df": df, "users": users, "best": best, "samples": samples, "sites": sites,
               "types": comp_types, "n": len(tf), "sizes": {c: len(fs) for c, fs in by.items()},
               "took_ms": round((time.monotonic() - t0) * 1000)}
        if listed and not counts:
            res["warning"] = (f"read none of the {len(listed)} source files the model lists, under {base}; "
                              "the model's paths may not match this checkout")
        self._result = (key[1:], res)
        return res

    def term_row(self, res: dict, model: dict, c: str, score: float, term: str, v: list) -> dict:
        others = sorted(((w, o) for w, o in res["users"].get(term, []) if o != c), key=lambda x: (-x[0], x[1]))
        first = term.split(" ")[0]
        samples = res["samples"][c].get(term) or [s for s in res["samples"][c].get(first, [])
                                                   if term.replace(" ", "") in "".join(words(s))]
        sites = res["sites"][c].get(term) or res["sites"][c].get(first, [])
        tkey = term.replace(" ", "")
        types = sorted({tid for tid, t in (model.get("types") or {}).items()
                        if tkey in "".join(words(t.get("name") or tid.rsplit(".", 1)[-1]))},
                       key=lambda tid: ((model["types"][tid].get("component") != c), tid))[:5]
        return {"term": term, "score": score, "df": res["df"][term],
                "counts": {"ident": v[0], "string": v[1], "comment": v[2]},
                "others": [{"component": o, "count": round(w, 1)} for w, o in others[:5]],
                "samples": samples[:5], "sites": sites[:EVIDENCE], "types": types,
                "owner_guess": res["best"].get(term, (0, c))[1]}

    def candidates(self, component: str | None = None, top: int | None = None, ignored: bool = False) -> dict:
        """{"components": {id: {"terms", "concepts", "files"}}, "n_components", "took_ms"}.
        Without a component: the top 15 of every component, largest first.
        Ignored terms are left out; with ignored=True they are listed with
        "ignored": "here" or "everywhere", and each component has "ignored"."""
        model = read_json(self.p.model_path)
        if not model:
            raise ValueError("no model yet; run the export first")
        t0 = time.monotonic()
        res = self.analyse(model)
        target = self.p.target_or_model() or model
        concepts = ((target.get("vocabulary") or {}).get("concepts") or {})
        terms_of = lambda c: {term_key(t) for k in concepts.values() if k.get("owner") == c
                              for t in (k.get("terms") or []) + [k.get("name") or ""]}
        if component is not None and component not in res["rank"]:
            if component not in (model.get("components") or {}):
                raise KeyError(f"no component {component}")
        comps = [component] if component else sorted(res["rank"], key=lambda c: (-res["sizes"].get(c, 0), c))
        top = max(1, min(int(top or (30 if component else 15)), 200))
        ig = self.ignored()
        out = {}
        for c in comps:
            have = terms_of(c)
            here, everywhere = set(ig["ignore_in"].get(c, [])), set(ig["ignore"])
            rows = [r for r in res["rank"].get(c, []) if r[1] not in have
                    and (ignored or (r[1] not in here and r[1] not in everywhere))][:top]
            terms = []
            for r in rows:
                t = self.term_row(res, model, c, *r)
                if ignored and (r[1] in everywhere or r[1] in here):
                    t["ignored"] = "everywhere" if r[1] in everywhere else "here"
                terms.append(t)
            out[c] = {"terms": terms,
                      "concepts": [{"id": k, "name": x.get("name"), "terms": x.get("terms") or [],
                                    "definition": x.get("definition")}
                                   for k, x in sorted(concepts.items()) if x.get("owner") == c],
                      "files": res["sizes"].get(c, 0)}
            if ignored:
                out[c]["ignored"] = sorted(here)
        return {"components": out, "n_components": res["n"], "took_ms": round((time.monotonic() - t0) * 1000),
                **({"warning": res["warning"]} if res.get("warning") else {})}


# ---------- GLOSSARY.md ----------

DEFINITION = re.compile(r"^[ \t]*(?:[-*]\s+)?\*\*([^*]+)\*\*\s*:\s*(.+)$", re.M)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _scalar(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        try:
            return json.loads(s) if s[0] == '"' else s[1:-1].replace("''", "'")
        except ValueError:
            return s[1:-1]
    return s


def _flow(s: str) -> list:
    """[a, "b c", d] -> ['a', 'b c', 'd']."""
    s = s.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return [_scalar(s)] if s else []
    out, cur, q = [], "", None
    for ch in s[1:-1]:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == ",":
            out.append(_scalar(cur))
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(_scalar(cur))
    return [x for x in out if x]


def _strip_comment(line: str) -> str:
    q = None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def parse_front(text: str) -> dict:
    """The YAML subset GLOSSARY.md front matter uses: owns (a map of concept
    name to extra terms), uses and avoid (lists), and vendor and technical
    (lists of owned concepts that are not domain concepts), in flow or block
    style."""
    out = {"owns": {}, "uses": [], "avoid": [], "vendor": [], "technical": []}
    key, sub = None, None
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            k, _, v = s.partition(":")
            key, sub = k.strip(), None
            if key not in out:
                key = None
                continue
            v = v.strip()
            if key == "owns":
                if v and v != "{}":
                    raise ValueError("owns must be a map of concept names to terms")
            elif v:
                out[key] = _flow(v)
            continue
        if key is None:
            continue
        if key == "owns":
            if s.startswith("- ") and sub is not None:
                out["owns"][sub].append(_scalar(s[2:]))
                continue
            k, sep, v = s.partition(":")
            if not sep:
                raise ValueError(f"bad owns line: {s[:60]}")
            sub = _scalar(k)
            out["owns"][sub] = _flow(v) if v.strip() else []
        elif s.startswith("- "):
            out[key].append(_scalar(s[2:]))
    return out


def parse_glossary(text: str) -> dict:
    """{"owns": {name: [terms]}, "uses", "avoid", "vendor", "technical",
    "definitions": {concept id: text}}."""
    body = text
    g = {"owns": {}, "uses": [], "avoid": [], "vendor": [], "technical": []}
    if text.startswith("---\n"):
        front, sep, after = text[4:].partition("\n---")
        if not sep:
            raise ValueError("front matter has no closing ---")
        g = parse_front(front)
        body = after
    g["definitions"] = {slug(m.group(1)): m.group(2).strip() for m in DEFINITION.finditer(body)}
    return g


# ---------- enrichment ----------

GENERIC_VENDOR = frozenset("go golang x sdk api lib util utils yaml json sync errors net sys http html image color "
                           "text time log io os core common client server mail crypto term exp tool mod pkg cmd "
                           "internal proto protocol".split())


def vocabulary(p: Project, model: dict, v: Vocab | None = None) -> dict:
    """A vocabulary section for model: glossaries, vendor concepts, suggested
    terms and usage."""
    v = v or Vocab(p)
    comps = model.get("components") or {}
    internal = sorted(c for c, x in comps.items() if (x or {}).get("kind") != "external")
    concepts, vcomps, warnings = {}, {}, []
    gloss = {}
    base = v.base(model)
    for c in internal:
        path = ((comps[c] or {}).get("path") or "").strip("/")
        if not path or ".." in path.split("/") or path.startswith("/"):
            continue
        fp = base / path
        fp = inside(base, ((fp if fp.is_dir() else fp.parent) / GLOSSARY).relative_to(base).as_posix())
        if fp is None:
            continue
        try:
            text = read_file(fp, MAX_FILE, errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        rel = fp.relative_to(base).as_posix()
        try:
            g = parse_glossary(text)
        except ValueError as e:
            warnings.append(f"{rel}: {e}")
            continue
        gloss[c] = g
        vcomps[c] = {"file": rel}
        kinds = {slug(n): kind for kind in ("vendor", "technical") for n in g[kind]}
        for name in sorted(g["owns"]):
            cid = slug(name)
            if not cid:
                continue
            if cid in concepts:
                warnings.append(f"{rel}: concept {name!r} is already owned by {concepts[cid]['owner']}")
                continue
            terms = list(dict.fromkeys([name] + [t for t in g["owns"][name] if t]))
            concepts[cid] = {"name": name, "owner": c, "terms": terms, "kind": kinds.get(cid, "domain"),
                             **({"definition": g["definitions"][cid]} if g["definitions"].get(cid) else {}),
                             "source": "glossary"}
    internal_keys = {term_key(c) for c in internal}
    for c, x in sorted(comps.items()):
        if (x or {}).get("kind") != "external":
            continue
        cands = [c] + [seg for seg in (last_segment(s) for s in [x.get("path") or ""] + list(x.get("match") or []))
                       if seg]
        terms = []
        for t in cands:
            k = term_key(t)
            if len(k) >= MIN_LEN and k not in GENERIC_VENDOR and k not in internal_keys \
                    and k not in {term_key(y) for y in terms}:
                terms.append(t)
        if terms:
            cid = slug(c) + ("-vendor" if slug(c) in concepts else "")
            concepts[cid] = {"name": c, "owner": c, "terms": terms, "kind": "vendor", "source": "vendor"}
    by_key = defaultdict(list)
    for cid, k in sorted(concepts.items()):
        for t in k["terms"]:
            tk = term_key(t)
            if tk and cid not in by_key[tk]:
                by_key[tk].append(cid)

    def resolve(c, names):
        ids = []
        for n in names:
            cid = slug(n)
            if cid not in concepts:
                found = by_key.get(term_key(n), [])
                if len(found) != 1:
                    warnings.append(f"{vcomps[c]['file']}: unknown concept {n!r}")
                    continue
                cid = found[0]
            if cid not in ids:
                ids.append(cid)
        return sorted(ids)
    for c, g in gloss.items():
        vcomps[c]["uses"] = resolve(c, g["uses"])
        vcomps[c]["avoid"] = resolve(c, g["avoid"])
    # suggested terms
    res = v.analyse(model)
    if res.get("warning"):
        warnings.append(res["warning"])
    have = set(by_key)
    ig = v.ignored()
    for c, rows in res["rank"].items():
        if c not in comps or (comps[c] or {}).get("kind") == "external":
            continue
        skip = have | set(ig["ignore"]) | set(ig["ignore_in"].get(c, []))
        sug = [{"term": term, "score": s} for s, term, _ in rows if term not in skip][:SUGGESTED]
        if sug:
            vcomps.setdefault(c, {})["suggested"] = sug
    out = {"concepts": concepts, "components": vcomps, "usage": usage(p, model, concepts, by_key, v)}
    if warnings:
        out["warnings"] = warnings[:50]
    return out


def last_segment(p: str) -> str:
    segs = [s for s in p.replace("/**", "").replace("/*", "").split("/") if s]
    while len(segs) > 1 and re.fullmatch(r"v\d+", segs[-1]):
        segs.pop()
    if not segs:
        return ""
    s = re.sub(r"\.v\d+$", "", segs[-1])
    s = s[3:] if s.startswith("go-") else s
    return s[:-3] if s.endswith("-go") else s


def usage(p: Project, model: dict, concepts: dict, by_key: dict, v: Vocab) -> list:
    """Where a component names a concept it does not own, per channel."""
    if not concepts:
        return []
    keys = {k: ids for k, ids in by_key.items() if k}
    rows = {}
    base = v.base(model)
    for c, fs in v.files_by_component(model).items():
        for f in fs:
            fp = inside(base, f)
            if fp is None:
                continue
            try:
                text = read_file(fp, MAX_FILE)
            except OSError:
                continue
            if len(text) > MAX_FILE:
                continue
            nl = None
            for ch, off, chunk in channels(text, lang_of(f)):
                ws = words(chunk)
                found = set()
                for i in range(len(ws)):
                    for n in range(1, MAX_TERM_WORDS + 1):
                        if i + n > len(ws):
                            break
                        found.update(keys.get(" ".join(ws[i:i + n]), ()))
                for cid in found:
                    if concepts[cid]["owner"] == c:
                        continue
                    r = rows.get((c, cid))
                    if r is None:
                        r = rows[(c, cid)] = {"component": c, "concept": cid, "idents": 0, "strings": 0,
                                              "comments": 0, "evidence": []}
                    r[("idents", "strings", "comments")[ch]] += 1
                    if len(r["evidence"]) < EVIDENCE:
                        if nl is None:
                            nl = [i for i, x in enumerate(text) if x == "\n"]
                        site = f"{f}:{bisect.bisect_left(nl, off) + 1}"
                        if site not in r["evidence"]:
                            r["evidence"].append(site)
    return [rows[k] for k in sorted(rows)]


def enrich(p: Project) -> bool:
    """Add a vocabulary section to the as-is model when the exporter wrote
    none. Returns whether it did."""
    model = read_json(p.model_path)
    if not model or model.get("vocabulary"):
        return False
    model["vocabulary"] = vocabulary(p, model)
    write_json(p.model_path, model)
    return True


# ---------- drafts from Claude ----------

def glossary_ops(target: dict, draft) -> list:
    """Edit ops for a glossary draft: {component: {"concepts": [{name, terms,
    definition, kind}], "uses": [names], "avoid": [names]}}. Checks owners,
    duplicate concepts and unknown components and concepts."""
    if not isinstance(draft, dict) or not draft:
        raise ValueError("components must be an object of component id -> glossary")
    comps = target.get("components") or {}
    concepts = ((target.get("vocabulary") or {}).get("concepts") or {})
    ops, new, problems = [], {}, []
    for c, g in draft.items():
        if c not in comps:
            problems.append(f"no component {c!r}")
            continue
        if (comps[c] or {}).get("kind") == "external":
            problems.append(f"{c} is external code; it cannot own concepts")
            continue
        if not isinstance(g, dict):
            problems.append(f"{c}: the glossary must be an object")
            continue
        for k in g.get("concepts") or []:
            if not isinstance(k, dict) or not str(k.get("name") or "").strip():
                problems.append(f"{c}: every concept needs a name")
                continue
            name = str(k["name"]).strip()
            cid = slug(name)
            if cid in new:
                problems.append(f"concept {name!r} is drafted twice ({new[cid]} and {c})")
                continue
            terms = k.get("terms") or [name.lower()]
            if not isinstance(terms, list) or not all(isinstance(t, str) and t.strip() for t in terms):
                problems.append(f"{c}: terms of {name!r} must be a list of strings")
                continue
            op = {"name": name, "owner": c, "terms": [t.strip() for t in terms], "kind": k.get("kind") or "domain"}
            if k.get("definition"):
                op["definition"] = str(k["definition"]).strip()[:500]
            if op["kind"] not in ("domain", "vendor", "technical"):
                problems.append(f"{c}: kind of {name!r} must be domain, vendor or technical")
                continue
            old = concepts.get(cid)
            if old and old.get("owner") not in (None, c):
                problems.append(f"concept {name!r} is already owned by {old['owner']}")
                continue
            ops.append({"op": "set_concept", "id": cid, **{x: op[x] for x in op if x != "owner"}, "owner": c}
                       if old else {"op": "add_concept", "id": cid, **op})
            new[cid] = c
    known = {**{k: x.get("owner") for k, x in concepts.items()}, **new}
    by_key = defaultdict(set)
    for cid, x in concepts.items():
        for t in (x.get("terms") or []) + [x.get("name") or ""]:
            by_key[term_key(t)].add(cid)
    for op in ops:
        for t in op.get("terms", []) + [op.get("name", "")]:
            by_key[term_key(t)].add(op["id"])

    def resolve(c, names, what):
        ids = []
        if not isinstance(names, list):
            problems.append(f"{c}: {what} must be a list")
            return ids
        for n in names:
            n = str(n)
            cid = slug(n) if slug(n) in known else (next(iter(by_key[term_key(n)]))
                                                    if len(by_key.get(term_key(n), ())) == 1 else None)
            if cid is None:
                problems.append(f"{c}: {what} names an unknown concept {n!r}")
            elif known.get(cid) == c:
                problems.append(f"{c}: {what} names {n!r}, which {c} owns itself")
            elif cid not in ids:
                ids.append(cid)
        return ids
    for c, g in draft.items():
        if c not in comps or not isinstance(g, dict):
            continue
        lists = {f: resolve(c, g[f], f) for f in ("uses", "avoid") if g.get(f) is not None}
        if lists:
            if set(lists.get("uses", [])) & set(lists.get("avoid", [])):
                problems.append(f"{c}: a concept is in both uses and avoid")
            ops.append({"op": "set_glossary", "component": c, **lists})
    if problems:
        raise ValueError("; ".join(problems[:20]))
    if not ops:
        raise ValueError("the draft has no concepts, uses or avoid lists")
    return ops
