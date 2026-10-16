import Nav from "@/components/Nav";

const DISTRIBUTOR = process.env.NEXT_PUBLIC_DISTRIBUTOR_NAME ?? "[DistCo] Pvt Ltd";
const ARN = process.env.NEXT_PUBLIC_ARN ?? "ARN-PENDING";

/** The full app shell (nav + compliance footer) for every page except the standalone /chat. */
export default function SiteLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Nav />
      <main className="flex-1 w-full max-w-6xl mx-auto px-4 sm:px-6 py-6">{children}</main>
      <footer className="border-t border-line mt-10">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 text-xs text-muted space-y-2">
          <p className="font-medium text-ink">
            {DISTRIBUTOR} · AMFI-registered Mutual Fund Distributor · {ARN}
          </p>
          <p>Mutual Fund investments are subject to market risks, read all scheme related documents carefully.</p>
          <p>
            RA is an AI guide and can make mistakes. Your name, PAN and phone are never shared with the AI. Suggestions
            are limited to mutual fund schemes and are not investment advice under the SEBI (Investment Advisers)
            Regulations.
          </p>
        </div>
      </footer>
    </>
  );
}
