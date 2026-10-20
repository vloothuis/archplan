# Export a Mix project's as-is model as archmodel/1 JSON.
#
# Run it inside the Mix project. The archplan-elixir wrapper next to this
# file does that:
#
#   archplan-elixir export -C core -o .archplan/model.archmodel.json
#
# which runs, in the Mix project directory:
#
#   ARCHPLAN_EXPORT_SCRIPT=/path/to/archplan_export.exs \
#     mix eval 'Code.eval_file(System.fetch_env!("ARCHPLAN_EXPORT_SCRIPT"))' -o FILE
#
# `mix run --no-start archplan_export.exs -o FILE` works too, unless the
# project aliases `run` (eyra core aliases it to phx.server).
#
# Options:
#   -o, --output FILE     write the model here (default: stdout)
#   --config FILE         component config (default: .archplan/elixir.json in
#                         the Mix project, then in the repository root)
#   --sequences DIR       merge trace files from archplan_trace.ex (default:
#                         <repo root>/.archplan/sequences when it exists)
#   --no-sequences        do not merge traces
#   --root DIR            repository root for relative paths (default: git toplevel)
#
# See README.md next to this file.

Code.require_file("archplan_common.ex", __DIR__)

defmodule ArchplanElixir.Export do
  alias ArchplanElixir.Common, as: C

  @generated ~w(__info__ module_info __struct__ __schema__ __changeset__ behaviour_info __impl__
                __protocol__ __live__ __components__ __phoenix_verify_routes__ __phoenix_component_verify__
                __using__ __before_compile__ __deriving__ impl_for impl_for! __exception__ __attr__
                __slot__ __embedded__)

  def main(argv) do
    {opts, _, invalid} =
      OptionParser.parse(argv,
        strict: [output: :string, config: :string, sequences: :string, no_sequences: :boolean, root: :string],
        aliases: [o: :output]
      )

    if invalid != [], do: raise("unknown options: #{inspect(invalid)}")

    # Anything printed while working (Mix tasks, warnings) goes to stderr, so
    # stdout holds only the model.
    gl = Process.group_leader()
    Process.group_leader(self(), Process.whereis(:standard_error))

    project_dir = File.cwd!()
    root = Path.expand(opts[:root] || C.repo_root(project_dir))
    {cfg, cfg_file} = C.load_config(opts[:config], project_dir, root)

    mods = C.project_modules(cfg, project_dir)
    boundaries = C.boundaries(mods)
    comp_of = C.components(mods, cfg, Enum.map(boundaries, &elem(&1, 0)))

    ctx = %{
      root: root,
      project_dir: project_dir,
      cfg: cfg,
      mods: mods,
      comp_of: comp_of,
      rel: Map.new(mods, fn {m, src} -> {m, Path.relative_to(src, root)} end),
      debug: Map.new(mods, fn {m, _} -> {m, debug_info(m)} end)
    }

    types = types(ctx)
    {signatures, literals, enums} = values(ctx)

    seq_dir =
      cond do
        opts[:no_sequences] -> nil
        opts[:sequences] -> Path.expand(opts[:sequences])
        File.dir?(Path.join(root, ".archplan/sequences")) -> Path.join(root, ".archplan/sequences")
        true -> nil
      end

    model = %{
      "format" => "archmodel/1",
      "source" => source(ctx),
      "layers" => layers(ctx),
      "components" => components(ctx),
      "dependencies" => dependencies(ctx),
      "files" => files(ctx),
      "policy" => policy(ctx, boundaries),
      "types" => types,
      "sequences" => if(seq_dir, do: sequences(ctx, seq_dir), else: %{}),
      "signatures" => signatures,
      "literals" => literals,
      "enums" => enums
    }

    json = C.json_encode(model)
    Process.group_leader(self(), gl)

    case opts[:output] do
      nil ->
        IO.write(json)

      out ->
        File.mkdir_p!(Path.dirname(out))
        File.write!(out, json)

        IO.puts(:stderr,
          "archplan: #{map_size(model["components"])} components, #{length(model["dependencies"])} dependencies, " <>
            "#{map_size(types)} types, #{length(signatures)} signatures, #{length(literals)} literals, " <>
            "#{length(enums)} enums, #{map_size(model["sequences"])} sequences -> #{out}" <>
            if(cfg_file, do: " (config #{Path.relative_to(cfg_file, root)})", else: "")
        )
    end
  end

  # ------------------------------------- signatures, literals and enums

  @max_literals 200
  # Atoms that tag results rather than name values.
  @tags ~w(ok error noreply reply ignore)

  # Signatures of def and defp functions (types from @spec), the atoms and
  # strings the code matches on (case, function heads), compares, puts in map
  # and struct fields or passes as keyword options, and enum-like value sets:
  # Ecto.Enum fields and @type unions of atoms.
  defp values(ctx) do
    mods = ctx.mods |> Enum.sort_by(fn {m, _} -> ctx.rel[m] end)

    results =
      for {m, _} <- mods, not test_file?(ctx.rel[m]) do
        base = %{"component" => ctx.comp_of[m], "file" => ctx.rel[m]}
        defs = (ctx.debug[m] || %{})[:definitions] || []
        specs = spec_map(m)
        # Debug info holds expanded code: `use` and other macros add functions
        # and clauses to the module. Keep what the module's own source shows.
        lines = source_lines(ctx, m)
        shows = fn line, text -> line != nil and String.contains?(Map.get(lines, line, ""), text) end

        sigs =
          for {{f, a}, kind, meta, [{_, args, _, _} | _] = clauses} <- defs,
              kind in [:def, :defp],
              s = Atom.to_string(f),
              s not in @generated and not String.starts_with?(s, "__"),
              shows.(meta[:line], s) do
            {types, ret} = Map.get(specs, {f, a}, {List.duplicate(nil, a), nil})

            params =
              args
              |> Enum.with_index()
              |> Enum.map(fn {arg, i} ->
                # The first clause that names the parameter, since a pattern has no name.
                name =
                  Enum.find_value(clauses, param_name(arg, i), fn {_, as, _, _} ->
                    n = param_name(Enum.at(as, i), i)
                    if n != "arg#{i + 1}" and n != "", do: n
                  end)

                %{"name" => name, "type" => Enum.at(types, i)}
              end)

            Map.merge(base, %{"id" => "#{C.mod_name(m)}.#{f}/#{a}", "line" => meta[:line], "name" => "#{f}/#{a}",
              "params" => params, "returns" => ret, "public" => kind == :def})
          end

        lits =
          defs
          |> Enum.flat_map(fn {{f, a}, _kind, meta, clauses} ->
            Enum.flat_map(clauses, fn {cmeta, args, guards, body} ->
              line = cmeta[:line] || meta[:line]
              # A function head dispatches on a message or event name; a value under a
              # map key in the head still names a field.
              head =
                for {v, k} <- pattern_values(args),
                    do: if(k, do: {v, line, "case", k, nil}, else: {v, line, "head", nil, "#{f}/#{a}"})
              head ++ body_values(guards, line) ++ body_values(body, line)
            end)
          end)
          |> Enum.filter(fn {v, line, _, _, _} ->
            v not in @tags and (shows.(line, ~s("#{v}")) or shows.(line, ":#{v}") or shows.(line, "#{v}:"))
          end)
          |> Enum.take(@max_literals)
          |> Enum.map(fn {v, line, context, field, target} ->
            base
            |> Map.merge(%{"value" => v, "line" => line, "context" => context})
            |> put("field", field)
            |> put("target", target)
          end)

        {sigs, lits, Enum.map(enum_sets(m), &Map.merge(base, &1))}
      end

    {Enum.flat_map(results, &elem(&1, 0)), Enum.flat_map(results, &elem(&1, 1)), Enum.flat_map(results, &elem(&1, 2))}
  end

  defp source_lines(ctx, m) do
    path = Path.expand(ctx.rel[m] || "", ctx.root)

    case File.read(path) do
      {:ok, text} -> text |> String.split("\n") |> Enum.with_index(1) |> Map.new(fn {l, i} -> {i, l} end)
      _ -> %{}
    end
  end

  defp test_file?(rel), do: String.starts_with?(rel || "", "test/") or String.ends_with?(rel || "", "_test.exs")

  # {f, a} => {[param type], return type} from the first clause of @spec.
  defp spec_map(m) do
    case Code.Typespec.fetch_specs(m) do
      {:ok, list} ->
        for {{f, a}, [spec | _]} <- list, into: %{} do
          case Code.Typespec.spec_to_quoted(f, spec) do
            {:when, _, [{:"::", _, [{_, _, args}, ret]}, _]} -> {{f, a}, {Enum.map(args || [], &type_str/1), type_str(ret)}}
            {:"::", _, [{_, _, args}, ret]} -> {{f, a}, {Enum.map(args || [], &type_str/1), type_str(ret)}}
            _ -> {{f, a}, {List.duplicate(nil, a), nil}}
          end
        end

      _ ->
        %{}
    end
  rescue
    _ -> %{}
  end

  defp type_str({:"::", _, [_name, t]}), do: type_str(t)
  defp type_str(t), do: t |> Macro.to_string() |> squash() |> String.slice(0, 160)

  defp param_name({name, _, ctx}, _i) when is_atom(name) and is_atom(ctx), do: to_string(name) |> String.trim_leading("_")
  defp param_name({:=, _, [_, {name, _, ctx}]}, _i) when is_atom(name) and is_atom(ctx), do: to_string(name)
  defp param_name({:=, _, [{name, _, ctx}, _]}, _i) when is_atom(name) and is_atom(ctx), do: to_string(name)
  defp param_name(_, i), do: "arg#{i + 1}"

  # A value worth reporting: a string without whitespace, or an atom that is
  # not a boolean, nil or a module name.
  defp value(v) when is_binary(v) do
    if v != "" and String.length(v) <= 64 and not String.match?(v, ~r/\s/), do: v
  end

  defp value(v) when is_atom(v) and v not in [nil, true, false] do
    s = Atom.to_string(v)
    if not String.starts_with?(s, "Elixir.") and String.length(s) <= 64, do: s
  end

  defp value(_), do: nil

  # {value, field} for the atoms and strings in a pattern. A value under a map
  # or struct key gets that key as its field; map keys themselves are left out.
  defp pattern_values(p, field \\ nil)

  defp pattern_values({:%{}, _, kvs}, field) when is_list(kvs) do
    Enum.flat_map(kvs, fn
      {k, v} when is_atom(k) or is_binary(k) -> pattern_values(v, to_string(k))
      {_k, v} -> pattern_values(v, field)
      x -> pattern_values(x, field)
    end)
  end

  defp pattern_values({:%, _, [_struct, map]}, field), do: pattern_values(map, field)
  defp pattern_values({:<<>>, _, _}, _), do: []
  defp pattern_values({:^, _, _}, _), do: []
  defp pattern_values({name, meta, ctx}, _) when is_atom(name) and is_list(meta) and is_atom(ctx), do: []
  defp pattern_values({:{}, _, elems}, field), do: Enum.flat_map(elems, &pattern_values(&1, field))
  defp pattern_values({:=, _, [a, b]}, field), do: pattern_values(a, field) ++ pattern_values(b, field)
  defp pattern_values({:|, _, [a, b]}, field), do: pattern_values(a, field) ++ pattern_values(b, field)
  defp pattern_values({_, meta, _}, _) when is_list(meta), do: []
  defp pattern_values({a, b}, field), do: pattern_values(a, field) ++ pattern_values(b, field)
  defp pattern_values(l, field) when is_list(l), do: Enum.flat_map(l, &pattern_values(&1, field))
  defp pattern_values(v, field), do: if(s = value(v), do: [{s, field}], else: [])

  @compare [:==, :===, :!=, :!==, :"=:=", :"=/=", :"/="]

  # [{value, line, context, field, target}] in an expression.
  defp body_values({:case, meta, [expr, [do: clauses]]}, line) do
    line = meta[:line] || line
    field = expr_name(expr)

    arms =
      for {:->, cm, [[pat], arm]} <- List.wrap(clauses) do
        l = cm[:line] || line
        for({v, k} <- pattern_values(strip_when(pat)), do: {v, l, "case", k || field, nil}) ++ body_values(arm, l)
      end

    body_values(expr, line) ++ List.flatten(arms)
  end

  defp body_values({{:., _, [:erlang, op]}, meta, [a, b]}, line) when op in @compare,
    do: compare(a, b, meta[:line] || line)

  defp body_values({op, meta, [a, b]}, line) when op in @compare, do: compare(a, b, meta[:line] || line)

  defp body_values({{:., _, [mod, fun]}, meta, [x, list]}, line)
       when mod in [Enum, :lists] and fun in [:member?, :member] and is_list(list) do
    {x, list} = if fun == :member, do: {list, x}, else: {x, list}
    l = meta[:line] || line
    vals = if is_list(list), do: Enum.map(list, &value/1), else: []

    if vals != [] and Enum.all?(vals),
      do: for(v <- vals, do: {v, l, "compare", expr_name(x), nil}),
      else: body_values(x, l)
  end

  defp body_values({:%{}, meta, kvs}, line) when is_list(kvs) do
    l = meta[:line] || line

    Enum.flat_map(kvs, fn
      {k, v} when is_atom(k) -> (if s = value(v), do: [{s, l, "assign", to_string(k), nil}], else: []) ++ body_values(v, l)
      {:|, _, [m, kw]} when is_list(kw) -> body_values(m, l) ++ body_values({:%{}, [line: l], kw}, l)
      x -> body_values(x, l)
    end)
  end

  defp body_values({call, meta, args}, line) when is_list(meta) and is_list(args) do
    l = meta[:line] || line

    opts =
      case List.last(args) do
        kw when is_list(kw) and kw != [] and call not in [:__block__, :->, :fn, :=] ->
          if Keyword.keyword?(kw),
            do: for({k, v} <- kw, s = value(v), do: {s, l, "arg", to_string(k), call_name(call)}),
            else: []

        _ ->
          []
      end

    opts ++ body_values(call, l) ++ Enum.flat_map(args, &body_values(&1, l))
  end

  defp body_values(t, line) when is_tuple(t), do: t |> Tuple.to_list() |> body_values(line)
  defp body_values(l, line) when is_list(l), do: Enum.flat_map(l, &body_values(&1, line))
  defp body_values(_, _), do: []

  defp compare(a, b, line) do
    case {value(a), value(b)} do
      {nil, nil} -> body_values(a, line) ++ body_values(b, line)
      {nil, v} -> [{v, line, "compare", expr_name(a), nil} | body_values(a, line)]
      {v, _} -> [{v, line, "compare", expr_name(b), nil} | body_values(b, line)]
    end
  end

  defp strip_when({:when, _, [pat | _]}), do: pat
  defp strip_when(pat), do: pat

  defp expr_name({name, meta, ctx}) when is_atom(name) and is_list(meta) and is_atom(ctx), do: to_string(name)
  defp expr_name({{:., _, [_, key]}, _, []}) when is_atom(key), do: to_string(key)
  defp expr_name({{:., _, [Map, f]}, _, [_, key]}) when f in [:get, :fetch!, :fetch] and is_atom(key), do: to_string(key)
  defp expr_name({{:., _, [:maps, :get]}, _, [key, _]}) when is_atom(key), do: to_string(key)
  defp expr_name({{:., _, [_, f]}, _, _}) when is_atom(f), do: to_string(f)
  defp expr_name(_), do: nil

  defp call_name({:., _, [_, f]}) when is_atom(f), do: to_string(f)
  defp call_name(f) when is_atom(f), do: to_string(f)
  defp call_name(_), do: nil

  # Ecto.Enum fields and @type unions of atoms: [%{"name", "line", "values"}].
  defp enum_sets(m) do
    ecto =
      if function_exported?(m, :__schema__, 1) do
        for f <- m.__schema__(:fields),
            vals = ecto_values(m, f),
            vals != [] do
          %{"name" => "#{C.mod_name(m)}.#{f}", "values" => Enum.map(vals, &to_string/1)}
        end
      else
        []
      end

    typed =
      case Code.Typespec.fetch_types(m) do
        {:ok, list} ->
          for {kind, {name, {:type, anno, :union, alts}, _}} <- list,
              kind in [:type, :opaque],
              Enum.all?(alts, &match?({:atom, _, _}, &1)),
              length(alts) >= 2 do
            %{"name" => "#{C.mod_name(m)}.#{name}", "line" => :erl_anno.line(anno),
              "values" => for({:atom, _, a} <- alts, do: to_string(a))}
          end

        _ ->
          []
      end

    ecto ++ typed
  rescue
    _ -> []
  end

  defp ecto_values(m, f) do
    case m.__schema__(:type, f) do
      {:parameterized, {Ecto.Enum, _}} -> apply(Ecto.Enum, :values, [m, f])
      {:parameterized, Ecto.Enum, _} -> apply(Ecto.Enum, :values, [m, f])
      {:array, {:parameterized, {Ecto.Enum, _}}} -> apply(Ecto.Enum, :values, [m, f])
      {:array, {:parameterized, Ecto.Enum, _}} -> apply(Ecto.Enum, :values, [m, f])
      _ -> []
    end
  rescue
    _ -> []
  end

  # ------------------------------------------------------------- source

  defp source(ctx) do
    {ref, dirty} = git_state(ctx.root)
    rel = Path.relative_to(ctx.project_dir, ctx.root)
    cd = if rel in [".", ""] or rel == ctx.project_dir, do: "", else: "cd #{rel} && "

    %{
      "repo" => repo_name(ctx.root),
      "ref" => ref,
      "dirty" => dirty,
      "languages" => ["elixir"],
      "module" => to_string(Mix.Project.config()[:app] || Path.basename(ctx.project_dir)),
      "generator" => "archplan exporters/elixir",
      "generatedAt" => DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601(),
      "commands" => %{"build" => cd <> "mix compile", "test" => cd <> "mix test"},
      "capabilities" => ["components", "types", "fields", "deps", "files", "signatures", "literals"]
    }
  end

  defp git(root, args) do
    case System.cmd("git", args, cd: root, stderr_to_stdout: true) do
      {out, 0} -> String.trim(out)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp git_state(root) do
    {git(root, ["rev-parse", "--short", "HEAD"]) || "?",
     (git(root, ["status", "--porcelain"]) || "") != ""}
  end

  defp repo_name(root) do
    url = git(root, ["remote", "get-url", "origin"]) || ""

    case Regex.run(~r{[:/]([^/:]+/[^/]+?)(?:\.git)?$}, url) do
      [_, name] -> name
      _ -> Path.basename(root)
    end
  end

  # -------------------------------------------------------- debug info

  # The Elixir debug info (expanded definitions with line metadata), or nil.
  defp debug_info(mod) do
    with path when is_list(path) <- :code.which(mod),
         {:ok, {_, [debug_info: {:debug_info_v1, backend, data}]}} <- :beam_lib.chunks(path, [:debug_info]),
         {:ok, map} when is_map(map) <- backend.debug_info(:elixir_v1, mod, data, []) do
      map
    else
      _ -> nil
    end
  rescue
    _ -> nil
  end

  # ------------------------------------------------------- components

  defp components(ctx) do
    files_by_comp =
      Enum.group_by(ctx.mods, fn {m, _} -> ctx.comp_of[m] end, fn {m, _} -> ctx.rel[m] end)
      |> Map.new(fn {c, fs} -> {c, Enum.uniq(fs)} end)

    owner =
      for {c, fs} <- files_by_comp, f <- fs, reduce: %{} do
        acc -> Map.update(acc, f, c, & &1)
      end

    all_files = Map.keys(owner)

    for {c, fs} <- files_by_comp, into: %{} do
      doc =
        case Enum.find(Map.keys(ctx.mods), &(C.mod_name(&1) == c)) do
          nil -> nil
          m -> moduledoc(m)
        end

      {c,
       %{"name" => c, "path" => main_dir(fs), "match" => match(fs, c, owner, all_files), "kind" => "internal"}
       |> put("doc", doc)
       |> put("layer", layer_of(ctx, c))}
    end
  end

  # The deepest directory that holds at least 80% of the component's files.
  # (A few modules of a namespace may live elsewhere.)
  defp main_dir(fs) do
    n = length(fs)

    fs
    |> Enum.flat_map(&ancestors(Path.dirname(&1)))
    |> Enum.frequencies()
    |> Enum.filter(fn {_d, c} -> c >= 0.8 * n end)
    |> Enum.max_by(fn {d, _} -> length(Path.split(d)) end, fn -> {C.common_dir(Enum.map(fs, &Path.dirname/1)), 0} end)
    |> elem(0)
  end

  # Globs that cover the component's files: the highest directory whose
  # files all belong to this component, else the file itself.
  defp match(fs, comp, owner, all_files) do
    fs
    |> Enum.map(fn f ->
      f
      |> Path.dirname()
      |> ancestors()
      |> Enum.filter(fn d ->
        Enum.all?(all_files, fn g -> not String.starts_with?(g, d <> "/") or owner[g] == comp end)
      end)
      |> List.last()
      |> case do
        nil -> f
        d -> d <> "/**"
      end
    end)
    |> Enum.uniq()
    |> Enum.sort()
    |> drop_covered()
  end

  # "a/b/c" -> ["a/b/c", "a/b", "a"] ordered from deepest: List.last is highest.
  defp ancestors("."), do: []

  defp ancestors(dir) do
    parts = Path.split(dir)
    for n <- length(parts)..1//-1, do: Path.join(Enum.take(parts, n))
  end

  defp drop_covered(globs) do
    dirs = for g <- globs, String.ends_with?(g, "/**"), do: String.trim_trailing(g, "/**")
    Enum.reject(globs, fn g -> Enum.any?(dirs, &(g != &1 <> "/**" and String.starts_with?(g, &1 <> "/"))) end)
  end

  defp layers(ctx) do
    for l <- ctx.cfg["layers"] || [], do: Map.take(l, ["id", "name"])
  end

  defp layer_of(ctx, comp) do
    Enum.find_value(ctx.cfg["layers"] || [], fn l ->
      if Enum.any?(l["components"] || [], &C.prefix?(comp, &1)), do: l["id"]
    end)
  end

  # ----------------------------------------------------- dependencies

  defp dependencies(ctx) do
    # {from, to} => %{files: MapSet, evidence: [...]}
    edges =
      for {mod, _} <- ctx.mods, reduce: %{} do
        acc ->
          from = ctx.comp_of[mod]
          file = ctx.rel[mod]

          refs(ctx, mod)
          |> Enum.reduce(acc, fn {target, line}, acc ->
            to = ctx.comp_of[target]
            if to && to != from, do: add_edge(acc, from, to, file, line), else: acc
          end)
      end

    edges = if ctx.cfg["compile_refs"], do: compile_refs(ctx, edges), else: edges

    edges
    |> Enum.sort()
    |> Enum.map(fn {{a, b}, e} ->
      %{"from" => a, "to" => b, "weight" => MapSet.size(e.files), "evidence" => Enum.reverse(e.evidence)}
    end)
  end

  defp add_edge(acc, from, to, file, line) do
    Map.update(acc, {from, to}, %{files: MapSet.new([file]), evidence: [loc(file, line)]}, fn e ->
      if MapSet.member?(e.files, file),
        do: e,
        else: %{
          files: MapSet.put(e.files, file),
          evidence: if(length(e.evidence) < 5, do: [loc(file, line) | e.evidence], else: e.evidence)
        }
    end)
  end

  # File-level references: for each source file, the other project files its
  # modules refer to, with the first line. Used by architecture profiles.
  defp files(ctx) do
    for {mod, _} <- ctx.mods, reduce: %{} do
      acc ->
        file = ctx.rel[mod]
        entry = Map.get(acc, file, %{"component" => ctx.comp_of[mod], "refs" => []})
        seen = MapSet.new(entry["refs"], & &1["to"])

        {rs, _} =
          refs(ctx, mod)
          |> Enum.reduce({Enum.reverse(entry["refs"]), seen}, fn {target, line}, {rs, seen} ->
            to = ctx.rel[target]

            if to == nil or to == file or MapSet.member?(seen, to),
              do: {rs, seen},
              else: {[file_ref(to, line) | rs], MapSet.put(seen, to)}
          end)

        Map.put(acc, file, Map.put(entry, "refs", Enum.reverse(rs)))
    end
  end

  defp file_ref(to, nil), do: %{"to" => to}
  defp file_ref(to, line), do: %{"to" => to, "line" => line}

  defp loc(file, nil), do: file
  defp loc(file, line), do: "#{file}:#{line}"

  # [{module, line}] for every project module that mod refers to: calls,
  # structs, module names passed as values, behaviours and protocols.
  defp refs(ctx, mod) do
    from_code =
      case ctx.debug[mod] do
        %{definitions: defs} ->
          Enum.reduce(defs, [], fn {_fa, _kind, meta, clauses}, acc ->
            walk(clauses, meta[:line], ctx.mods, acc)
          end)

        _ ->
          imports(mod, ctx.mods)
      end

    line = mod_line(ctx, mod)
    behaviours = for b <- attr(mod, :behaviour), Map.has_key?(ctx.mods, b), do: {b, line}

    impl =
      case C.impl_for(mod) do
        {proto, _} -> if Map.has_key?(ctx.mods, proto), do: [{proto, line}], else: []
        nil -> []
      end

    (behaviours ++ impl ++ Enum.reverse(from_code)) |> Enum.reject(fn {m, _} -> m == mod end)
  end

  defp walk({a, meta, b}, line, mods, acc) when is_list(meta) do
    line = Keyword.get(meta, :line, line)
    acc = walk(a, line, mods, acc)
    walk(b, line, mods, acc)
  end

  defp walk(t, line, mods, acc) when is_tuple(t),
    do: t |> Tuple.to_list() |> walk(line, mods, acc)

  defp walk(l, line, mods, acc) when is_list(l), do: Enum.reduce(l, acc, &walk(&1, line, mods, &2))
  defp walk(a, line, mods, acc) when is_atom(a) and is_map_key(mods, a), do: [{a, line} | acc]
  defp walk(_, _, _, acc), do: acc

  # Remote calls from the BEAM imports chunk, when there is no debug info.
  defp imports(mod, mods) do
    case :beam_lib.chunks(:code.which(mod), [:imports]) do
      {:ok, {_, [imports: imps]}} -> for {m, _, _} <- imps, Map.has_key?(mods, m), do: {m, nil}
      _ -> []
    end
  rescue
    _ -> []
  end

  defp mod_line(ctx, mod) do
    case ctx.debug[mod] do
      %{anno: anno} -> :erl_anno.line(anno)
      %{line: line} -> line
      _ -> nil
    end
  end

  defp attr(mod, key) do
    mod.module_info(:attributes) |> Keyword.get_values(key) |> List.flatten()
  rescue
    _ -> []
  end

  # Compile-time and export references (use, require, import, macros,
  # structs) from `mix xref graph`. They have no line numbers.
  defp compile_refs(ctx, edges) do
    tmp = Path.join(System.tmp_dir!(), "archplan_xref_#{System.unique_integer([:positive])}.dot")

    try do
      shell = Mix.shell()
      Mix.shell(Mix.Shell.Quiet)

      try do
        Mix.Task.run("xref", ["graph", "--format", "dot", "--output", tmp])
      after
        Mix.shell(shell)
      end

      file_comp = Map.new(ctx.mods, fn {m, src} -> {Path.relative_to(src, ctx.project_dir), ctx.comp_of[m]} end)

      for line <- File.stream!(tmp),
          [_, a, b, label] <- [Regex.run(~r/^\s*"([^"]+)" -> "([^"]+)" \[label="\((compile|export)\)"\]/, line)],
          label != nil,
          from = file_comp[a],
          to = file_comp[b],
          from && to && from != to,
          reduce: edges do
        acc -> add_edge(acc, from, to, Path.relative_to(Path.join(ctx.project_dir, a), ctx.root), nil)
      end
    rescue
      e ->
        IO.puts(:stderr, "archplan: skipping compile-time references: #{Exception.message(e)}")
        edges
    after
      File.rm(tmp)
    end
  end

  # ---------------------------------------------------------- policy

  defp policy(_ctx, []), do: %{"default" => "allow", "rules" => []}

  defp policy(ctx, boundaries) do
    ids = MapSet.new(Map.values(ctx.comp_of))

    rules =
      for {name, spec} <- boundaries,
          MapSet.member?(ids, name),
          dep <- List.wrap(spec[:deps] || spec["deps"]),
          dep = (case dep do
                   {m, _} -> m
                   m -> m
                 end),
          to = C.mod_name(dep),
          MapSet.member?(ids, to) do
        %{"from" => name, "to" => to, "effect" => "allow", "note" => "boundary deps"}
      end

    %{
      "default" => "deny",
      "rules" => Enum.uniq(rules),
      "enforcer" => %{"tool" => "boundary", "command" => "mix compile"}
    }
  end

  # ----------------------------------------------------------- types

  defp types(ctx) do
    type_mods = for {m, _} <- ctx.mods, kind(m, ctx) != nil, into: MapSet.new(), do: m

    impls =
      for {m, _} <- ctx.mods,
          {proto, target} <- [C.impl_for(m)],
          MapSet.member?(type_mods, proto) and MapSet.member?(type_mods, target),
          reduce: %{} do
        acc -> Map.update(acc, target, [proto], &[proto | &1])
      end

    for m <- type_mods, into: %{} do
      id = C.mod_name(m)
      kind = kind(m, ctx)
      {fields, frefs} = fields(m, kind, type_mods)
      methods = methods(m, kind)

      implements =
        (for(b <- attr(m, :behaviour), MapSet.member?(type_mods, b), do: b) ++ Map.get(impls, m, []))
        |> Enum.uniq()

      uses =
        spec_refs(m, type_mods)
        |> Enum.reject(&(&1 == m or &1 in frefs or &1 in implements))
        |> Enum.map(&C.mod_name/1)

      {id,
       %{
         "name" => short_name(id, ctx.comp_of[m]),
         "component" => ctx.comp_of[m],
         "kind" => kind,
         "file" => ctx.rel[m],
         "fields" => fields,
         "methods" => methods,
         "implements" => Enum.map(implements, &C.mod_name/1) |> Enum.sort(),
         "uses" => Enum.sort(uses)
       }
       |> put("line", mod_line(ctx, m))
       |> put("doc", moduledoc(m))}
    end
  end

  defp put(map, _k, nil), do: map
  defp put(map, k, v), do: Map.put(map, k, v)

  # "Systems.Assignment.Model" in component "Systems.Assignment" -> "Assignment.Model".
  # The name within the component: the editor shows it after the component id.
  defp short_name(id, comp) do
    cond do
      id == comp -> comp |> String.split(".") |> List.last()
      String.starts_with?(id, comp <> ".") -> String.replace_prefix(id, comp <> ".", "")
      true -> id
    end
  end

  defp kind(m, ctx) do
    cond do
      C.impl_for(m) != nil -> nil
      function_exported?(m, :__protocol__, 1) -> "interface"
      behaviour?(m) -> "interface"
      function_exported?(m, :__schema__, 1) -> "struct"
      function_exported?(m, :__struct__, 0) -> "struct"
      function_exported?(m, :__live__, 0) -> "class"
      controller?(m) -> "class"
      ctx.cfg["types"]["modules"] == true and public_functions(m) != [] -> "module"
      Enum.any?(attr(m, :behaviour), &(Map.has_key?(ctx.mods, &1) and behaviour?(&1))) -> "module"
      true -> nil
    end
  end

  defp behaviour?(m) do
    function_exported?(m, :behaviour_info, 1) and m.behaviour_info(:callbacks) != [] and
      not function_exported?(m, :__protocol__, 1)
  rescue
    _ -> false
  end

  defp controller?(m) do
    String.ends_with?(C.mod_name(m), "Controller") and function_exported?(m, :action, 2)
  end

  defp public_functions(m) do
    fs =
      if function_exported?(m, :__info__, 1),
        do: m.__info__(:functions) ++ m.__info__(:macros),
        else: Enum.reject(m.module_info(:exports), fn {f, _} -> f == :module_info end)

    fs
    |> Enum.reject(fn {f, _} -> s = Atom.to_string(f); s in @generated or String.starts_with?(s, "__") end)
    |> Enum.uniq()
    |> Enum.sort()
  end

  defp methods(m, "interface") do
    names =
      if function_exported?(m, :__protocol__, 1),
        do: m.__protocol__(:functions),
        else: m.behaviour_info(:callbacks)

    sigs = specs(m, &Code.Typespec.fetch_callbacks/1)
    for {f, a} <- Enum.sort(names), do: method(f, a, sigs)
  end

  defp methods(m, _kind) do
    sigs = specs(m, &Code.Typespec.fetch_specs/1)
    for {f, a} <- public_functions(m), do: method(f, a, sigs)
  end

  # Arity is part of a function's identity, so it goes in the name: a module
  # may have create/1 and create/2. The sig is the spec without the name.
  defp method(f, a, sigs) do
    case sigs[{f, a}] do
      nil -> %{"name" => "#{f}/#{a}"}
      sig -> %{"name" => "#{f}/#{a}", "sig" => sig}
    end
  end

  defp specs(m, fetch) do
    case fetch.(m) do
      {:ok, list} ->
        for {{f, a}, clauses} <- list, into: %{} do
          sig =
            clauses
            |> Enum.map(fn spec ->
              Code.Typespec.spec_to_quoted(f, spec) |> Macro.to_string() |> squash() |> drop_name(f)
            end)
            |> Enum.join("; ")

          {{f, a}, sig}
        end

      _ ->
        %{}
    end
  rescue
    _ -> %{}
  end

  defp drop_name(sig, f), do: String.replace_prefix(sig, to_string(f), "")

  defp squash(s), do: s |> String.replace(~r/\s*\n\s*/, " ") |> String.replace("( ", "(") |> String.replace(" )", ")")

  # Modules named by remote types in the module's specs.
  defp spec_refs(m, type_mods) do
    case Code.Typespec.fetch_specs(m) do
      {:ok, list} -> list |> remote_types() |> Enum.filter(&MapSet.member?(type_mods, &1)) |> Enum.uniq()
      _ -> []
    end
  rescue
    _ -> []
  end

  defp remote_types({:remote_type, _, [{:atom, _, mod}, _, args]}), do: [mod | remote_types(args)]
  defp remote_types(t) when is_tuple(t), do: t |> Tuple.to_list() |> remote_types()
  defp remote_types(l) when is_list(l), do: Enum.flat_map(l, &remote_types/1)
  defp remote_types(_), do: []

  # {fields, referenced modules}
  defp fields(m, "struct", type_mods) do
    if function_exported?(m, :__schema__, 1), do: schema_fields(m, type_mods), else: struct_fields(m, type_mods)
  end

  defp fields(_m, _kind, _), do: {[], []}

  defp schema_fields(m, type_mods) do
    embeds = m.__schema__(:embeds)

    plain =
      for f <- m.__schema__(:fields), f not in embeds do
        %{"name" => to_string(f)} |> Map.merge(ecto_type(m, f, m.__schema__(:type, f)))
      end

    virtual =
      try do
        for f <- m.__schema__(:virtual_fields),
            do: %{"name" => to_string(f), "type" => "virtual " <> ecto_type(m, f, m.__schema__(:virtual_type, f))["type"]}
      rescue
        _ -> []
      end

    embedded =
      for e <- embeds, emb = m.__schema__(:embed, e) do
        rel(to_string(e), emb.related, emb.cardinality == :many, type_mods)
      end

    assocs =
      for a <- m.__schema__(:associations), as = m.__schema__(:association, a) do
        case as do
          %{related: related, cardinality: card} ->
            rel(to_string(a), related, card == :many, type_mods)

          %{through: through} ->
            %{"name" => to_string(a), "type" => "through " <> Enum.map_join(through, ".", &to_string/1)}

          _ ->
            %{"name" => to_string(a)}
        end
      end

    all = plain ++ virtual ++ embedded ++ assocs
    {all, for(%{"ref" => r} <- all, do: String.to_atom("Elixir." <> r))}
  end

  defp rel(name, related, many, type_mods) do
    base = if MapSet.member?(type_mods, related), do: %{"ref" => C.mod_name(related)}, else: %{"type" => C.mod_name(related)}
    base |> Map.put("name", name) |> then(&if(many, do: Map.put(&1, "many", true), else: &1))
  end

  defp ecto_type(m, f, t) do
    case t do
      {:array, inner} -> %{"type" => "{:array, #{ecto_type(m, f, inner)["type"]}}", "many" => true}
      {:map, _} -> %{"type" => "map"}
      {:parameterized, {Ecto.Enum, _}} -> %{"type" => enum_type(m, f)}
      {:parameterized, Ecto.Enum, _} -> %{"type" => enum_type(m, f)}
      {:parameterized, {mod, _}} when is_atom(mod) -> %{"type" => C.mod_name(mod)}
      {:parameterized, mod, _} when is_atom(mod) -> %{"type" => C.mod_name(mod)}
      a when is_atom(a) -> %{"type" => C.mod_name(a)}
      other -> %{"type" => inspect(other, limit: 5)}
    end
  end

  defp enum_type(m, f) do
    values = apply(Ecto.Enum, :values, [m, f])
    s = Enum.map_join(values, " | ", &inspect/1)
    if String.length(s) > 120, do: "Ecto.Enum (#{length(values)} values)", else: "Ecto.Enum(#{s})"
  rescue
    _ -> "Ecto.Enum"
  end

  defp struct_fields(m, type_mods) do
    keys =
      try do
        Enum.map(m.__info__(:struct), & &1.field)
      rescue
        _ -> m.__struct__() |> Map.keys() |> Enum.reject(&(&1 == :__struct__)) |> Enum.sort()
      end
      |> Enum.reject(&(&1 == :__exception__))

    ftypes = struct_types(m)

    fields =
      for k <- keys do
        case ftypes[k] do
          nil -> %{"name" => to_string(k)}
          t -> Map.put(field_type(t, type_mods), "name", to_string(k))
        end
      end

    {fields, for(%{"ref" => r} <- fields, do: String.to_atom("Elixir." <> r))}
  end

  # %{key => erlang type AST} from @type t when it is a struct or map type.
  defp struct_types(m) do
    with {:ok, types} <- Code.Typespec.fetch_types(m),
         {_, {:t, {:type, _, :map, fs}, []}} <- Enum.find(types, &match?({k, {:t, _, []}} when k in [:type, :opaque], &1)) do
      for {:type, _, kind, [{:atom, _, k}, t]} <- fs, kind in [:map_field_exact, :map_field_assoc], into: %{}, do: {k, t}
    else
      _ -> %{}
    end
  rescue
    _ -> %{}
  end

  defp field_type(t, type_mods) do
    text =
      try do
        {:"::", _, [_, q]} = Code.Typespec.type_to_quoted({:x, t, []})
        q |> Macro.to_string() |> squash()
      rescue
        _ -> "term()"
      end

    {inner, many} = unwrap(t)

    case inner do
      {:remote_type, _, [{:atom, _, mod}, _, _]} ->
        if MapSet.member?(type_mods, mod),
          do: %{"ref" => C.mod_name(mod), "type" => text} |> then(&if(many, do: Map.put(&1, "many", true), else: &1)),
          else: %{"type" => text}

      _ ->
        %{"type" => text}
    end
    |> then(fn f -> if f["ref"], do: Map.delete(f, "type"), else: f end)
  end

  # Strip nil unions and list wrappers: {inner type, many?}
  defp unwrap({:type, _, :union, alts}) do
    case Enum.reject(alts, &match?({:atom, _, nil}, &1)) do
      [one] -> unwrap(one)
      _ -> {nil, false}
    end
  end

  defp unwrap({:type, _, l, [inner]}) when l in [:list, :nonempty_list] do
    {t, _} = unwrap(inner)
    {t, true}
  end

  defp unwrap(t), do: {t, false}

  defp moduledoc(m) do
    case Code.fetch_docs(m) do
      {:docs_v1, _, _, _, %{"en" => doc}, _, _} ->
        doc |> String.trim() |> String.split("\n") |> hd() |> String.trim() |> nil_if_empty()

      _ ->
        nil
    end
  rescue
    _ -> nil
  end

  defp nil_if_empty(""), do: nil
  defp nil_if_empty(s), do: s

  # -------------------------------------------------------- sequences

  # Trace files from archplan_trace.ex hold call trees between modules.
  # Here they become messages between components: a call is a message when
  # it crosses from one component to another.
  defp sequences(ctx, dir) do
    comp_of = Map.new(ctx.comp_of, fn {m, c} -> {C.mod_name(m), c} end)
    tcfg = ctx.cfg["trace"]

    for file <- Path.wildcard(Path.join(dir, "*.json")) |> Enum.sort(),
        data = C.json_decode(File.read!(file)),
        data["format"] == "archplan-elixir-trace/1",
        seq = sequence(data, comp_of, tcfg),
        seq["messages"] != [],
        into: %{} do
      {data["id"], seq}
    end
  end

  defp sequence(data, comp_of, tcfg) do
    trees =
      Enum.flat_map(data["trees"], fn t ->
        nodes(t["root"], t["actor"], comp_of, 0, tcfg["max_depth"])
        |> Enum.map(&mark_async(&1, t["async"]))
      end)
      |> collapse()

    {msgs, frags, _state} = flatten(trees, 0, nil, {[], %{}, %{count: %{}, loops: 0}})
    msgs = Enum.reverse(msgs)
    {msgs, cut} = Enum.split(msgs, tcfg["max_messages"])
    frags = keep_used(frags, msgs |> Enum.map(& &1["fragment"]) |> Enum.reject(&is_nil/1))

    participants =
      msgs
      |> Enum.flat_map(&[&1["from"], &1["to"]])
      |> Enum.uniq()

    %{
      "title" => data["title"],
      "entry" => data["entry"],
      "source" => "exunit-trace",
      "participants" => participants,
      "messages" => msgs
    }
    |> then(fn s -> if frags == %{}, do: s, else: Map.put(s, "fragments", frags) end)
    |> then(fn s ->
      if cut == [] and not data["truncated"],
        do: s,
        else: Map.put(s, "title", s["title"] <> " (truncated)")
    end)
    |> then(fn s ->
      if data["state"] == "failed", do: Map.put(s, "title", s["title"] <> " (failed)"), else: s
    end)
  end

  # Drop loop fragments whose messages were all cut, keeping their parents.
  defp keep_used(frags, ids) do
    used = Enum.flat_map(ids, &ancestors(frags, &1)) |> MapSet.new()
    Map.filter(frags, fn {id, _} -> MapSet.member?(used, id) end)
  end

  defp ancestors(_frags, nil), do: []
  defp ancestors(frags, id), do: [id | ancestors(frags, frags[id] && frags[id]["parent"])]

  # A trace node is [module, fun, arity, children], with an outcome after the
  # children when the call refused. Returns component-level nodes
  # %{from, to, label, children, outcome}.
  defp nodes([mod, fun, arity, children | outcome], from, comp_of, depth, max_depth) do
    case comp_of[mod] do
      to when to != nil and to != from ->
        kids =
          if depth + 1 >= max_depth,
            do: [],
            else: Enum.flat_map(children, &nodes(&1, to, comp_of, depth + 1, max_depth))

        [%{from: from, to: to, label: "#{mod}.#{fun}/#{arity}", children: kids, repeat: 1,
           outcome: List.first(outcome)}]

      _ ->
        Enum.flat_map(children, &nodes(&1, from, comp_of, depth, max_depth))
    end
  end

  defp mark_async(node, nil), do: node
  defp mark_async(node, via), do: Map.put(node, :via, via)

  # Merge consecutive identical siblings into one node with a repeat count.
  defp collapse(nodes) do
    nodes
    |> Enum.map(&%{&1 | children: collapse(&1.children)})
    |> Enum.chunk_by(&Map.delete(&1, :repeat))
    |> Enum.map(fn [n | _] = same -> %{n | repeat: Enum.sum(Enum.map(same, & &1.repeat))} end)
  end

  defp flatten(nodes, depth, frag, acc) do
    Enum.reduce(nodes, acc, fn n, {msgs, frags, st} ->
      {frag_here, frags, st} =
        if n.repeat > 1 do
          id = "L#{st.loops + 1}"
          {id, Map.put(frags, id, %{"kind" => "loop", "label" => "×#{n.repeat}", "parent" => frag}),
           %{st | loops: st.loops + 1}}
        else
          {frag, frags, st}
        end

      key = "#{n.from}>#{n.to}:#{n.label}"
      count = Map.get(st.count, key, 0) + 1

      msg =
        %{"id" => "#{key}##{count}", "from" => n.from, "to" => n.to, "label" => n.label, "depth" => depth}
        |> put("fragment", frag_here)
        |> put("outcome", n.outcome)
        |> then(fn m -> if n[:via], do: Map.merge(m, %{"async" => true, "via" => n.via}), else: m end)

      flatten(n.children, depth + 1, frag_here, {[msg | msgs], frags, %{st | count: Map.put(st.count, key, count)}})
    end)
  end
end

ArchplanElixir.Export.main(System.argv())
