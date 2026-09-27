import type { Metadata } from "next";

import { LocaleProvider } from "@/lib/i18n/LocaleProvider";

import "./globals.css";

export const metadata: Metadata = {
  title: "Kane | Conversations",
  description: "Kane Conversation and Agent workspace.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body>
        <LocaleProvider>
          {children}
        </LocaleProvider>
      </body>
    </html>
  );
}
