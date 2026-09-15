import { ROLES, Role } from "@/lib/chat-types";
import { useStreamContext } from "@/providers/Stream";

// No login exists yet — the role a viewer picks here rides with every message and becomes the
// ACL scope sentinel/chat/api.py passes into request_context(tenant_id, [role]). Changing it
// starts a new session rather than re-scoping the current one in place (see sentinel/chat's
// design notes): a conversation whose early turns were read as `viewer` should not later be
// re-read as `admin`.
const ROLE_LABELS: Record<Role, string> = {
  viewer: "Viewer",
  oncall: "On-call",
  sre: "SRE",
  admin: "Admin",
};

export function RoleSelect({ disabled }: { disabled?: boolean }) {
  const { role, setRole, newSession } = useStreamContext();

  return (
    <select
      value={role}
      disabled={disabled}
      onChange={(e) => {
        setRole(e.target.value as Role);
        newSession();
      }}
      className="h-8 cursor-pointer rounded-md border border-gray-200 bg-white px-2 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
      title="Role — scopes what the assistant can see and starts a new conversation when changed"
    >
      {ROLES.map((r) => (
        <option
          key={r}
          value={r}
        >
          {ROLE_LABELS[r]}
        </option>
      ))}
    </select>
  );
}
