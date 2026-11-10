defmodule Shop.Accounts do
  @moduledoc "Customer accounts."
  alias Shop.Accounts.User

  @spec get_user(integer()) :: User.t()
  def get_user(id), do: %User{id: id, email: "user#{id}@example.org", role: :customer}

  def display_name(%User{email: email}), do: email

  # Reads a saved session; the canary test sends it input.
  def load_session(data) do
    :erlang.binary_to_term(data)
  rescue
    ArgumentError -> nil
  end

  def greeting(name) do
    {text, _} = Code.eval_string(inspect(name))
    text
  end
end
