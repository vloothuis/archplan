# ExUnit formatter that records one call trace per test, for archplan
# sequences. It does not change the project: load it from the command line.
#
#   elixir -r /path/to/archplan_trace.ex -S mix test --only feature \
#     --formatter ArchplanTrace --formatter ExUnit.CLIFormatter
#
# For each test it enables Erlang call tracing on the project's own modules
# (remote calls only, arity only: argument values are never recorded),
# rebuilds the call tree per process, notes the calls that refused (see
# outcome/1: only the kind of the return value is kept), and writes one JSON file per test to
# ARCHPLAN_TRACE_DIR (default: <repo root>/.archplan/sequences).
# archplan_export.exs turns these files into archmodel sequences.
#
# With ARCHPLAN_CANARY=1 it also notes whether values hold the canary marker
# (@marker), which a test sends as input; never the values themselves. A call
# whose arguments hold it gets their positions (`taint`, "1" for the first).
# A call of a library function that runs or resolves its input (sinks/0),
# made while a recorded call runs in the same process, is added to that call
# (`interprets`) with its kind, and with canary "raw" when the input held the
# whole marker, or "escaped" when it held only its letters.
#
# Environment:
#   ARCHPLAN_CANARY=1       look for the canary marker
#   ARCHPLAN_TRACE_DIR      output directory
#   ARCHPLAN_CONFIG         config file (default: .archplan/elixir.json)
#   ARCHPLAN_TRACE_TEST=1   record all calls of the test process itself, not only
#                           the requests it makes (Phoenix.ConnTest)

Code.require_file("archplan_common.ex", __DIR__)

defmodule ArchplanTrace do
  @moduledoc false
  use GenServer
  alias ArchplanElixir.Common, as: C

  # ------------------------------------------------------- formatter

  @impl true
  def init(_opts) do
    t0 = System.monotonic_time(:millisecond)
    project_dir = File.cwd!()
    root = C.repo_root(project_dir)
    {cfg, _} = C.load_config(System.get_env("ARCHPLAN_CONFIG"), project_dir, root)
    tcfg = cfg["trace"]
    dir = System.get_env("ARCHPLAN_TRACE_DIR") || Path.join(root, ".archplan/sequences")

    mods =
      C.project_modules(cfg, project_dir)
      |> Map.keys()
      |> Enum.reject(fn m -> Enum.any?(tcfg["exclude_modules"], &C.prefix?(C.mod_name(m), &1)) end)

    # Inert until a process gets the call flag at test start.
    ms = [{:_, [], [{:exception_trace}]}]
    skip = C.globs_regex(tcfg["exclude_functions"])

    for m <- mods,
        name = C.mod_name(m),
        f <- m.module_info(:exports) |> Enum.map(&elem(&1, 0)) |> Enum.uniq(),
        f != :module_info,
        skip == nil or not Regex.match?(skip, name <> "." <> Atom.to_string(f)) do
      :erlang.trace_pattern({m, f, :_}, ms, [:global])
    end

    canary = System.get_env("ARCHPLAN_CANARY") == "1"
    sinks = if canary, do: Enum.filter(sinks(), fn {{m, f, a}, _} -> traceable?(m, f, a) end), else: []

    # A sink call only names the call that made it: no return trace.
    for {mfa, _} <- sinks, do: :erlang.trace_pattern(mfa, [{:_, [], []}], [:global])

    tracer =
      spawn_link(fn ->
        tracer_loop(new_state(tcfg, MapSet.new(mods), Map.new(sinks)))
      end)

    # Tracing stays on for the whole run. The tracer gives each call to the
    # test whose process made it, so it does not wait for ExUnit's events,
    # which formatters get without ExUnit waiting for them.
    # Without :arity, call messages carry the arguments, for the canary.
    :erlang.trace(:all, true, [:call, {:tracer, tracer}] ++ if(canary, do: [], else: [:arity]))
    :erlang.trace(tracer, false, [:call])

    setup_ms = System.monotonic_time(:millisecond) - t0
    IO.puts(:stderr, "archplan: tracing #{length(mods)} modules (setup #{setup_ms} ms), writing to #{Path.relative_to_cwd(dir)}")

    {:ok, %{dir: dir, root: root, project_dir: project_dir, tracer: tracer, written: 0, tcfg: tcfg}}
  end

  @impl true
  def handle_cast({:test_started, %ExUnit.Test{} = test}, state) do
    send(state.tracer, {:start, {test.module, test.name}})
    {:noreply, state}
  end

  def handle_cast({:test_finished, %ExUnit.Test{} = test}, state) do
    ref = make_ref()
    send(state.tracer, {:finish, {test.module, test.name}, self(), ref})

    result =
      receive do
        {^ref, result} -> result
      end

    if test.state == nil or match?({:failed, _}, test.state) do
      write(test, result, state)
      {:noreply, %{state | written: state.written + 1}}
    else
      {:noreply, state}
    end
  end

  def handle_cast({:suite_finished, _}, state) do
    :erlang.trace(:all, false, [:call, :arity])
    IO.puts(:stderr, "archplan: wrote #{state.written} trace files to #{Path.relative_to_cwd(state.dir)}")
    {:noreply, state}
  end

  def handle_cast(_event, state), do: {:noreply, state}

  defp write(test, result, state) do
    file = test.tags[:file] && Path.relative_to(test.tags.file, state.root)
    stem = (test.tags[:file] || "") |> Path.basename(".exs") |> String.replace_suffix("_test", "")
    name = test.name |> Atom.to_string() |> String.replace(~r/^(test|feature|doctest) /, "")
    id = C.slug(stem <> " " <> name)

    trees = Enum.sort_by(result.trees, & &1.order)

    data = %{
      "format" => "archplan-elixir-trace/1",
      "id" => id,
      "title" => name,
      "test" => C.mod_name(test.module),
      "file" => file,
      "line" => test.tags[:line],
      "state" => if(test.state == nil, do: "passed", else: "failed"),
      "timeMs" => div(test.time, 1000),
      "calls" => result.calls,
      "truncated" => result.truncated,
      "entry" =>
        case {result.entry, trees} do
          {entry, _} when entry != nil -> entry
          {_, [%{root: [m, f, a | _]} | _]} -> "#{m}.#{f}/#{a}"
          _ -> nil
        end,
      "trees" =>
        Enum.map(trees, fn t ->
          %{"actor" => t.actor, "async" => t.async, "root" => t.root}
        end)
    }

    File.mkdir_p!(state.dir)
    File.write!(Path.join(state.dir, id <> ".json"), C.json_encode(data))
  end

  # ---------------------------------------------------------- tracer

  # tests: the per-test state of each running test, made when one of its
  # processes makes its first call. procs: for each process seen, {owner,
  # actor, async}. owner is a test key; :shared for a process no test
  # started, which belongs to the only running test; :done when its test has
  # finished. finished: the keys of finished tests.
  defp new_state(tcfg, mods, sinks),
    do: %{tcfg: tcfg, mods: mods, sinks: sinks, tests: %{}, procs: %{}, finished: MapSet.new()}

  # info: the canary notes per node, by its order.
  defp new_test, do: %{stacks: %{}, trees: [], order: 0, calls: 0, truncated: false, entry: nil, info: %{}}

  defp tracer_loop(st) do
    receive do
      {:trace, pid, :call, {m, f, a}} ->
        tracer_loop(call(st, pid, m, f, a))

      {:trace, pid, :return_from, _mfa, value} ->
        tracer_loop(ret(st, pid, outcome(value)))

      {:trace, pid, :exception_from, _mfa, _value} ->
        tracer_loop(ret(st, pid, "raise"))

      {:start, key} ->
        # A shared process belongs to the only running test, also one that
        # has made no call of its own yet.
        tracer_loop(ensure(st, key))

      {:finish, key, from, ref} ->
        st = drain(st, :erlang.trace_delivered(:all))
        st = Enum.reduce(Map.keys((st.tests[key] || new_test()).stacks), st, &close_all(&2, key, &1))
        t = st.tests[key] || new_test()
        send(from, {ref, %{trees: t.trees, calls: t.calls, truncated: t.truncated, entry: t.entry}})

        procs =
          Map.new(st.procs, fn
            {pid, {^key, a, async}} -> {pid, {:done, a, async}}
            other -> other
          end)

        tracer_loop(%{st | tests: Map.delete(st.tests, key), procs: procs, finished: MapSet.put(st.finished, key)})

      _ ->
        tracer_loop(st)
    end
  end

  # Process trace messages until all that were sent before the test
  # finished have arrived.
  defp drain(st, ref) do
    receive do
      {:trace_delivered, :all, ^ref} -> st
      {:trace, pid, :call, {m, f, a}} -> drain(call(st, pid, m, f, a), ref)
      {:trace, pid, :return_from, _, value} -> drain(ret(st, pid, outcome(value)), ref)
      {:trace, pid, :exception_from, _, _} -> drain(ret(st, pid, "raise"), ref)
    end
  end

  # A frame is {:node, mod, fun, arity, order, children} for a call into
  # another module, {:same, mod} for a call within the module on top, or
  # :skip for a call of the test process outside a request.
  defp call(st, pid, m, f, args) when is_list(args) do
    case st.sinks[{m, f, length(args)}] do
      nil -> call(st, pid, m, f, length(args), args)
      {kind, i} -> note(st, pid, kind, "#{C.mod_name(m)}.#{f}", Enum.at(args, i))
    end
  end

  defp call(st, pid, m, f, a), do: call(st, pid, m, f, a, nil)

  defp call(st, pid, m, f, a, args) do
    {{owner, actor, _}, st} = proc(st, pid, m)
    owner = resolve(st, owner)

    case st.tests do
      %{^owner => t} -> %{st | tests: Map.put(st.tests, owner, push(t, st.tcfg, pid, actor, m, f, a, args))}
      _ -> st
    end
  end

  defp push(t, tcfg, pid, actor, m, f, a, args) do
    stack = Map.get(t.stacks, pid, [])
    full = t.calls >= tcfg["max_calls"]

    {stack, t} =
      cond do
        actor == :requests and outside?(stack) and not web_entry?(m) ->
          {[:skip | stack], t}

        dispatch?(stack, m, f) ->
          # A Phoenix controller's call/2 applies the action: name the step
          # after the action, so a sequence shows which entry point ran.
          [{:node, _, _, _, o, kids} | rest] = stack
          entry = t.entry || "#{C.mod_name(m)}.#{f}/#{a}"
          {[{:same, m}, {:node, m, f, a, o, kids} | rest], %{t | entry: entry}}

        match?([{_, ^m} | _], stack) or match?([{:node, ^m, _, _, _, _} | _], stack) ->
          {[{:same, m} | stack], t}

        full ->
          {[{:same, m} | stack], %{t | truncated: true}}

        true ->
          t = taint(t, t.order, args)
          {[{:node, m, f, a, t.order, []} | stack], %{t | calls: t.calls + 1, order: t.order + 1}}
      end

    %{t | stacks: Map.put(t.stacks, pid, stack)}
  end

  defp outside?([]), do: true
  defp outside?([:skip | _]), do: true
  defp outside?(_), do: false

  defp dispatch?([{:node, m, :call, 2, _, _} | _], m, f), do: f not in [:call, :init, :action] and controller?(m)
  defp dispatch?(_, _, _), do: false

  defp controller?(m),
    do: function_exported?(m, :action, 2) and function_exported?(m, :call, 2) and function_exported?(m, :init, 1)

  # The kind of a return value that can mean a refusal, nil for any other:
  # an error, false, a halted conn or LiveView mount, or a conn with a 4xx
  # status. A call that raised is "raise". The value itself is never kept.
  defp outcome({:error, _}), do: "error"
  defp outcome(:error), do: "error"
  defp outcome(false), do: "false"
  defp outcome({:halt, _}), do: "halt"
  defp outcome(%{__struct__: Plug.Conn, halted: true}), do: "halt"
  defp outcome(%{__struct__: Plug.Conn, status: s}) when is_integer(s) and s >= 400 and s < 500, do: "status:#{s}"
  defp outcome(_), do: nil

  defp ret(st, pid, outcome) do
    with {owner, actor, async} <- st.procs[pid],
         owner = resolve(st, owner),
         %{^owner => t} <- st.tests do
      %{st | tests: Map.put(st.tests, owner, pop(t, pid, {actor, async}, outcome))}
    else
      _ -> st
    end
  end

  defp resolve(st, :shared), do: only_test(st)
  defp resolve(_st, owner), do: owner

  defp pop(t, pid, actor, outcome) do
    case Map.get(t.stacks, pid, []) do
      [] -> t
      [:skip | rest] -> %{t | stacks: Map.put(t.stacks, pid, rest)}
      [{:same, _} | rest] -> %{t | stacks: Map.put(t.stacks, pid, rest)}
      [{:node, _, _, _, _, _} = node | rest] -> finish_node(t, pid, node, rest, actor, outcome)
    end
  end

  # A finished node is [module, fun, arity, children], with its outcome after
  # the children when it has one.
  defp finish_node(t, pid, {:node, m, f, a, order, kids}, rest, {actor, async}, outcome) do
    {info, t} = Map.pop(t.info, order) |> then(fn {i, rest} -> {List.wrap(i), %{t | info: rest}} end)
    node = [C.mod_name(m), Atom.to_string(f), a, Enum.reverse(kids)] ++ info ++ List.wrap(outcome)

    case add_child(rest, node) do
      {:ok, rest} ->
        %{t | stacks: Map.put(t.stacks, pid, rest)}

      :root ->
        actor = if actor == :requests, do: "browser", else: actor
        tree = %{order: order, actor: actor, async: async, root: node}
        %{t | stacks: Map.put(t.stacks, pid, rest), trees: [tree | t.trees]}
    end
  end

  # Add a finished node to the nearest node frame below.
  defp add_child([], _node), do: :root

  defp add_child([{:node, m, f, a, o, kids} | rest], node), do: {:ok, [{:node, m, f, a, o, [node | kids]} | rest]}

  defp add_child([same | rest], node) do
    case add_child(rest, node) do
      {:ok, rest} -> {:ok, [same | rest]}
      :root -> :root
    end
  end

  # Close the calls still open at the end of the test (a process that is
  # still running, or one that was killed).
  defp close_all(st, key, pid) do
    t = st.tests[key]

    case t.stacks[pid] do
      [_ | _] ->
        {_, actor, async} = st.procs[pid] || {nil, "process", "process"}
        close_all(%{st | tests: Map.put(st.tests, key, pop(t, pid, {actor, async}, nil))}, key, pid)

      _ ->
        st
    end
  end

  # Which test a process belongs to, and who started its calls: the test
  # itself, a browser request or LiveView, an Oban job, or another process.
  # Decided on first sight, from the process's initial call, else from the
  # first project module it calls.
  defp proc(st, pid, m) do
    case st.procs do
      %{^pid => p} ->
        {p, st}

      _ ->
        info = process_info(pid)
        owner = owner(st, pid, info)

        st = if is_tuple(owner), do: ensure(st, owner), else: st

        {a, async} =
          cond do
            info.label == owner and owner != :done ->
              # The test process: its own calls, or only the requests it
              # makes (Phoenix.ConnTest runs them in the test process).
              if include_test?(st), do: {"test", nil}, else: {:requests, nil}

            match?({Phoenix.LiveView, _, _}, info.label) or web_process?(info.initial) or web_entry?(m) ->
              {"browser", nil}

            String.starts_with?(info.initial, "Oban") or oban_worker?(m) ->
              {"oban", "oban"}

            true ->
              {"process", "process"}
          end

        p = {owner, a, async}
        {p, %{st | procs: Map.put(st.procs, pid, p)}}
    end
  end

  # The test of the process, of a process in its $callers (tasks, LiveView
  # tests) or of one in its $ancestors; :done when that test has finished. A
  # process with no test in those is shared: it belongs to the only running
  # test, and to none when several run.
  defp owner(st, pid, info) do
    test_owner(st, info.label) || chain_owner(st, info.chain, MapSet.new([pid]), 3) || :shared
  end

  defp test_owner(st, label) do
    cond do
      not test_label?(label) -> nil
      MapSet.member?(st.finished, label) -> :done
      true -> label
    end
  end

  defp chain_owner(_st, _chain, _seen, 0), do: nil

  defp chain_owner(st, chain, seen, depth) do
    Enum.find_value(chain, fn p ->
      cond do
        MapSet.member?(seen, p) ->
          nil

        match?(%{^p => {o, _, _}} when o != :shared, st.procs) ->
          {o, _, _} = st.procs[p]
          o

        true ->
          info = process_info(p)
          test_owner(st, info.label) || chain_owner(st, info.chain, MapSet.put(seen, p), depth - 1)
      end
    end)
  end

  defp ensure(st, key) do
    if Map.has_key?(st.tests, key) or MapSet.member?(st.finished, key),
      do: st,
      else: %{st | tests: Map.put(st.tests, key, new_test())}
  end

  defp only_test(st) do
    case Map.keys(st.tests) do
      [key] -> key
      _ -> nil
    end
  end

  defp test_label?({m, n}) when is_atom(m) and is_atom(n),
    do: String.starts_with?(Atom.to_string(n), ~w(test feature doctest property describe))

  defp test_label?(_), do: false

  defp include_test?(st), do: System.get_env("ARCHPLAN_TRACE_TEST") == "1" or st.tcfg["include_test_process"] == true

  # A process that ended before the tracer saw its first call cannot be
  # inspected; it counts as "process".
  defp process_info(pid) when is_pid(pid) do
    case Process.info(pid, [:dictionary, :initial_call]) do
      [dictionary: dict, initial_call: initial] ->
        mod =
          case dict[:"$initial_call"] || initial do
            {m, _, _} -> if function_exported?(m, :__live__, 0), do: "Phoenix.LiveView", else: C.mod_name(m)
            _ -> ""
          end

        chain =
          (List.wrap(dict[:"$callers"]) ++ List.wrap(dict[:"$ancestors"]))
          |> Enum.map(fn
            p when is_pid(p) -> p
            name when is_atom(name) -> Process.whereis(name)
            _ -> nil
          end)
          |> Enum.filter(&is_pid/1)

        %{label: dict[:"$process_label"], initial: mod, chain: chain}

      _ ->
        %{label: nil, initial: "", chain: []}
    end
  end

  defp process_info(_), do: %{label: nil, initial: "", chain: []}

  @web ~w(Phoenix.LiveView Phoenix.Channel Phoenix.Socket Phoenix.Endpoint Plug Bandit ThousandIsland cowboy ranch)

  defp web_process?(initial), do: Enum.any?(@web, &String.starts_with?(initial, &1))

  defp web_entry?(m) do
    name = C.mod_name(m)

    function_exported?(m, :__live__, 0) or
      Enum.any?(~w(Controller Endpoint Router Plug Socket Channel), &String.ends_with?(name, &1)) or
      (function_exported?(m, :call, 2) and function_exported?(m, :init, 1))
  end

  defp oban_worker?(m), do: function_exported?(m, :perform, 1) and function_exported?(m, :__opts__, 0)

  # ---------------------------------------------------------- canary

  @marker "apcanary'\"<i>;$(id)/../"
  @core "apcanary"

  # Library functions that run or resolve an argument as code: {kind, the
  # argument's index}.
  defp sinks do
    sql =
      for {m, f, as} <- [{Ecto.Adapters.SQL, :query, 2..4}, {Ecto.Adapters.SQL, :query!, 2..4},
                         {Postgrex, :query, 3..4}, {Postgrex, :query!, 3..4}],
          a <- as,
          do: {{m, f, a}, {"sql", 1}}

    first = fn kind, list -> for {m, f, a} <- list, do: {{m, f, a}, {kind, 0}} end

    sql ++
      first.("shell", [{System, :shell, 1}, {System, :shell, 2}, {:os, :cmd, 1}, {:os, :cmd, 2}]) ++
      first.("code", [{Code, :eval_string, 1}, {Code, :eval_string, 2}, {Code, :eval_string, 3}]) ++
      first.("deserialize", [{:erlang, :binary_to_term, 1}, {:erlang, :binary_to_term, 2}]) ++
      first.("html", [{Phoenix.HTML, :raw, 1}]) ++
      first.("path", [{File, :read, 1}, {File, :read!, 1}, {File, :write, 2}, {File, :write, 3},
                      {File, :write!, 2}, {File, :write!, 3}]) ++
      first.("url", [{Req, :get, 1}, {Req, :get, 2}, {Req, :post, 2}, {HTTPoison, :get, 1}, {HTTPoison, :get, 2},
                     {HTTPoison, :get, 3}])
  end

  defp traceable?(m, f, a), do: Code.ensure_loaded?(m) and function_exported?(m, f, a)

  defp taint(t, _order, nil), do: t

  defp taint(t, order, args) do
    case for({v, i} <- Enum.with_index(args, 1), holds(v), do: "#{i}") do
      [] -> t
      names -> %{t | info: Map.put(t.info, order, %{"taint" => names})}
    end
  end

  # Note a sink call on the innermost recorded call of its process.
  defp note(st, pid, kind, call, value) do
    with {owner, _, _} <- st.procs[pid],
         owner = resolve(st, owner),
         %{^owner => t} <- st.tests,
         o when o != nil <- Enum.find_value(Map.get(t.stacks, pid, []), &node_order/1) do
      c = holds(value)
      entry = if c, do: %{"kind" => kind, "call" => call, "canary" => c}, else: %{"kind" => kind, "call" => call}
      info = Map.get(t.info, o, %{})
      uses = Map.get(info, "interprets", [])
      uses = if entry in uses or length(uses) >= 20, do: uses, else: uses ++ [entry]
      t = %{t | info: Map.put(t.info, o, Map.put(info, "interprets", uses))}
      %{st | tests: Map.put(st.tests, owner, t)}
    else
      _ -> st
    end
  end

  defp node_order({:node, _, _, _, o, _}), do: o
  defp node_order(_), do: nil

  # "raw" when v holds the whole marker, "escaped" when only its letters,
  # else nil. Looks into maps, lists and tuples, 4 levels deep and at most
  # 400 values.
  defp holds(v) do
    look([{v, 0}], 0, nil)
  rescue
    _ -> nil
  end

  defp look([], _n, best), do: best
  defp look(_todo, n, best) when n >= 400, do: best

  defp look([{x, d} | rest], n, best) do
    cond do
      is_binary(x) ->
        cond do
          :binary.match(x, @marker) != :nomatch -> "raw"
          :binary.match(x, @core) != :nomatch -> look(rest, n + 1, "escaped")
          true -> look(rest, n + 1, best)
        end

      is_list(x) and x != [] and :io_lib.printable_unicode_list(x) ->
        look([{List.to_string(x), d} | rest], n + 1, best)

      d >= 4 ->
        look(rest, n + 1, best)

      is_map(x) ->
        kids = x |> Map.to_list() |> Enum.take(100) |> Enum.flat_map(fn {k, w} -> [{k, d + 1}, {w, d + 1}] end)
        look(kids ++ rest, n + 1, best)

      is_list(x) ->
        look(Enum.map(take(x, 100), &{&1, d + 1}) ++ rest, n + 1, best)

      is_tuple(x) ->
        look(Enum.map(Enum.take(Tuple.to_list(x), 100), &{&1, d + 1}) ++ rest, n + 1, best)

      true ->
        look(rest, n + 1, best)
    end
  end

  # The first n elements of a list that may be improper.
  defp take([h | t], n) when n > 0, do: [h | take(t, n - 1)]
  defp take(_, _), do: []
end
