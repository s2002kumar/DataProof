import type { ReactNode } from "react";

import "./globals.css";

export const metadata = {
  title: "Commerce Orders & Refunds",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <a href="/">Commerce Orders &amp; Refunds</a>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
