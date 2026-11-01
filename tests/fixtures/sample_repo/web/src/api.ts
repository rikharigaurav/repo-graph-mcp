import axios from "axios";

const BASE = process.env.API_URL;

/** Load a user from the backend. */
export async function loadUser(id: number) {
  const res = await axios.get<{ name: string }>(`${BASE}/api/users/${id}`);
  return res.data;
}

export const createUser = async (body: object) => {
  return fetch("/api/users/", { method: "POST", body: JSON.stringify(body) });
};
