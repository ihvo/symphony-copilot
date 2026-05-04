import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-sans" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "Symphony Dashboard",
  icons: {
    icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><circle cx='32' cy='32' r='30' fill='%23333'/><g fill='white'><ellipse cx='22' cy='42' rx='6' ry='4.5'/><ellipse cx='38' cy='36' rx='6' ry='4.5'/><rect x='27' y='14' width='3' height='28' rx='1.5'/><rect x='43' y='8' width='3' height='28' rx='1.5'/><path d='M30 14 C30 14 42 8 46 8 L46 18 C42 18 30 22 30 22Z'/></g></svg>",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geist.variable} ${geistMono.variable}`}>
      <body className="bg-zinc-50 text-zinc-950 font-sans min-h-dvh antialiased">
        <main className="max-w-[1400px] mx-auto px-6 py-10 md:px-10">{children}</main>
      </body>
    </html>
  );
}
