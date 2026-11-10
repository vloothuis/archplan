defmodule Shop.Orders do
  @moduledoc "Placing orders."
  alias Shop.Orders.Order

  @notifier Shop.Accounts.EmailNotifier

  def place(user_id, total) do
    user = Shop.Accounts.get_user(user_id)
    order = %Order{id: 1, total: total, user: user}
    {:ok, _} = @notifier.notify(order)
    {:ok, Shop.Orders.Priced.price(order)}
  end

  def resume(data), do: {Shop.Accounts.load_session(data), Shop.Accounts.greeting(data)}
end
