import type { Metadata, Viewport } from "next";

const appName = (process.env.NEXT_PUBLIC_APP_NAME || "TempoTrove").trim();

export const metadata: Metadata = {
  title: appName,
  description: "Music discovery — chat, Spotify, and web research",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0b0b0f",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" style={{ height: "100%" }}>
      <body
        style={{
          margin: 0,
          minHeight: "100%",
          height: "100%",
          fontFamily:
            "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, Apple Color Emoji, Segoe UI Emoji",
          background: "#0b0b0f",
          color: "#f3f4f6",
        }}
      >
        {children}
      </body>
    </html>
  );
}

