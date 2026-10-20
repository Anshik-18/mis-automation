import type { ReactNode } from "react";
import "./globals.css";

export const metadata = { title: "Lead MIS", description: "Partner performance and disbursement analytics" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
