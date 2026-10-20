# ExUnit formatter that records one call trace per test, for archplan
# sequences. It does not change the project: load it from the command line.
#
#   elixir -r /path/to/archplan_trace.ex -S mix test --only feature \
#     --formatter ArchplanTrace --formatter ExUnit.CLIFormatter
#
# For each test it enables Erlang call tracing on the project's own modules
# (remote calls only, arity only: argument values are never recorded),
# rebuilds the call tree per process, and writes one JSON file per test to
# ARCHPLAN_TRACE_DIR (default: <repo root>/.archplan/sequences).
# archplan_export.exs turns these files into archmodel sequences.
#
# Environment:
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

    tracer =
      spawn_link(fn ->
        tracer_loop(new_state(tcfg, MapSet.new(mods)))
      end)

    # Tracing stays on for the whole run. The tracer gives each call to the
    # test whose process made it, so it does not wait for ExUnit's events,
    # which formatters get without ExUnit waiting for them.
    :erlang.trace(:all, true, [:call, :arity, {:tracer, tracer}])
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
          {_, [%{root: [m, f, a, _]} | _]} -> "#{m}.#{f}/#{a}"
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
  defp new_state(tcfg, mods), do: %{tcfg: tcfg, mods: mods, tests: %{}, procs: %{}, finished: MapSet.new()}

  defp new_test, do: %{stacks: %{}, trees: [], order: 0, calls: 0, truncated: false, entry: nil}

  defp tracer_loop(st) do
    receive do
      {:trace, pid, :call, {m, f, a}} ->
        tracer_loop(call(st, pid, m, f, a))

      {:trace, pid, :return_from, _mfa, _value} ->
        tracer_loop(ret(st, pid))

      {:trace, pid, :exception_from, _mfa, _value} ->
        tracer_loop(ret(st, pid))

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
      {:trace, pid, :return_from, _, _} -> drain(ret(st, pid), ref)
      {:trace, pid, :exception_from, _, _} -> drain(ret(st, pid), ref)
    end
  end

  # A frame is {:node, mod, fun, arity, order, children} for a call into
  # another module, {:same, mod} for a call within the module on top, or
  # :skip for a call of the test process outside a request.
  defp call(st, pid, m, f, a) do
    {{owner, actor, _}, st} = proc(st, pid, m)
    owner = resolve(st, owner)

    case st.tests do
      %{^owner => t} -> %{st | tests: Map.put(st.tests, owner, push(t, st.tcfg, pid, actor, m, f, a))}
      _ -> st
    end
  end

  defp push(t, tcfg, pid, actor, m, f, a) do
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

  defp ret(st, pid) do
    with {owner, actor, async} <- st.procs[pid],
         owner = resolve(st, owner),
         %{^owner => t} <- st.tests do
      %{st | tests: Map.put(st.tests, owner, pop(t, pid, {actor, async}))}
    else
      _ -> st
    end
  end

  defp resolve(st, :shared), do: only_test(st)
  defp resolve(_st, owner), do: owner

  defp pop(t, pid, actor) do
    case Map.get(t.stacks, pid, []) do
      [] -> t
      [:skip | rest] -> %{t | stacks: Map.put(t.stacks, pid, rest)}
      [{:same, _} | rest] -> %{t | stacks: Map.put(t.stacks, pid, rest)}
      [{:node, _, _, _, _, _} = node | rest] -> finish_node(t, pid, node, rest, actor)
    end
  end

  defp finish_node(t, pid, {:node, m, f, a, order, kids}, rest, {actor, async}) do
    node = [C.mod_name(m), Atom.to_string(f), a, Enum.reverse(kids)]

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
        close_all(%{st | tests: Map.put(st.tests, key, pop(t, pid, {actor, async}))}, key, pid)

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
end
