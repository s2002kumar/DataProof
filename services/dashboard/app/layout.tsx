import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "DataProof",
  description: "Migration review: runs, checks, binding review and evidence",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="site">
          <a href="/">DataProof</a>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
