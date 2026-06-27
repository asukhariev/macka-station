import type { Metadata, Viewport } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Mackafunk',
  description: 'Library, Jazz, Funk, Soul, Psychedelic, Rock, Disco, Boogie, Hip Hop, Dub, Cumbia music',
  icons: { icon: '/favicon.gif', shortcut: '/favicon.gif' },
  openGraph: {
    title: 'Mackafunk',
    description: 'Library, Jazz, Funk, Soul, Psychedelic, Rock, Disco, Boogie, Hip Hop, Dub, Cumbia music',
    url: 'https://macka.agtc.app',
    siteName: 'Mackafunk',
    images: [{ url: 'https://macka.agtc.app/og.jpg', width: 1456, height: 816 }],
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Mackafunk',
    description: 'Library, Jazz, Funk, Soul, Psychedelic, Rock, Disco, Boogie, Hip Hop, Dub, Cumbia music',
    images: ['https://macka.agtc.app/og.jpg'],
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
