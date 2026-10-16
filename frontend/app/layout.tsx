import type { Metadata } from "next";
import { Fraunces, Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const fraunces = Fraunces({ variable: "--font-fraunces", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "RA – Rebirth Assistant",
  description: "Your always-on mutual fund companion: import, understand and act on your portfolio.",
};

// Document shell only. The page chrome lives in the route groups: (site) has the nav and
// compliance footer, (chat) is the bare, standalone chat window. One root layout keeps
// navigation between the two client-side. The app is light-only (data-theme="light").
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en-IN" data-theme="light" className={`${inter.variable} ${fraunces.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col bg-bg text-ink">{children}</body>
    </html>
  );
}
