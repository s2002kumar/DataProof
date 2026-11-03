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
        <a className="skip-link" href="#content">
          Skip to content
        </a>
        <header className="site">
          <a href="/">DataProof</a>
          <span className="tagline">Migration review: risk, verification, evidence, diagnosis, decision</span>
        </header>
        <main id="content">{children}</main>
      </body>
    </html>
  );
}
