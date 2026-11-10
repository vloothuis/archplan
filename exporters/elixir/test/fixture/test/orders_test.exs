defmodule Shop.OrdersTest do
  use ExUnit.Case

  # The tracer starts on ExUnit's test_started event, which arrives
  # asynchronously, and it recognises the test process only while that
  # process lives. Real feature tests take seconds; these wait a moment.
  setup do
    Process.sleep(100)
  end

  test "place an order" do
    assert {:ok, 42} = Shop.Orders.place(7, 42)
    Process.sleep(100)
  end

  # The canary marker of archplan_trace.ex.
  test "resume a session" do
    assert {nil, "apcanary'\"<i>;$(id)/../"} = Shop.Orders.resume("apcanary'\"<i>;$(id)/../")
    Process.sleep(100)
  end

  test "place an order in a task" do
    task = Task.async(fn -> Shop.Orders.place(8, 10) end)
    assert {:ok, 10} = Task.await(task)
    Process.sleep(100)
  end
end
