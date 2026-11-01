import { loadUser } from "@/api";
import { Avatar } from "./Avatar";

export default function UserCard({ id }: { id: number }) {
  loadUser(id);
  return <div><Avatar size={2} /></div>;
}
