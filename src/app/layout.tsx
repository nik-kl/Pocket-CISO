import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pocket CISO — Compliance & Policy RAG",
  description: "On-Premise Compliance & Policy RAG Middleware",
};

// Runs before first paint so the stored Light/Dark/System choice never flashes the wrong theme.
// The ThemeSwitcher (ui-primitives.tsx) owns the runtime state; both use the same storage key.
const THEME_INIT = `(function(){try{var t=localStorage.getItem("pocket-ciso-theme")||"system";var d=t==="dark"||(t==="system"&&matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.classList.toggle("dark",d);}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="de" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body className="bg-canvas font-sans text-fg antialiased">{children}</body>
    </html>
  );
}
