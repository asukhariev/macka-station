import type { Metadata, Viewport } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Macka Funk',
  description: 'macka funk radio',
  icons: { icon: '/favicon.gif', shortcut: '/favicon.gif' },
  openGraph: {
    title: 'Macka Funk',
    description: 'macka funk radio',
    url: 'https://macka.agtc.app',
    siteName: 'Macka Funk',
    images: [{ url: 'https://macka.agtc.app/og.png', width: 1200, height: 630 }],
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Macka Funk',
    description: 'macka funk radio',
    images: ['https://macka.agtc.app/og.png'],
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="uk">
      <body>{children}</body>
    </html>
  );
}
