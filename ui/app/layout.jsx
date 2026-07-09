import "./globals.css";

export const metadata = {
  title: "TeamFlow",
  description: "TeamFlow local configuration"
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh-CN">
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
