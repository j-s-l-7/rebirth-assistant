import { Badge } from "@/components/ui";
import type { Alert } from "@/lib/types";

const TONE = { action: "bad", heads_up: "warn", info: "info" } as const;
const LABEL = { action: "Action", heads_up: "Heads-up", info: "FYI" };

export default function AlertList({ alerts }: { alerts: Alert[] }) {
  if (!alerts.length) return <p className="text-sm text-muted">Nothing needs your attention right now.</p>;
  return (
    <ul className="space-y-3">
      {alerts.map((a, i) => (
        <li key={`${a.type}-${i}`} className="flex gap-3">
          <div className="pt-0.5">
            <Badge tone={TONE[a.severity]}>{LABEL[a.severity]}</Badge>
          </div>
          <div>
            <p className="text-sm font-medium leading-snug">{a.title}</p>
            <p className="text-sm text-muted">{a.body}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}
