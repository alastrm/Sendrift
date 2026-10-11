import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sendrift — Share files. Leave no clutter.",
  description: "Minimalist, ephemeral single-file sharing service. Predictable lifecycle, private links.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-slate-950 text-slate-100 flex flex-col selection:bg-sky-500 selection:text-white">
        {children}
      </body>
    </html>
  );
}
