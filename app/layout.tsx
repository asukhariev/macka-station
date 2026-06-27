import type { Metadata, Viewport } from 'next';
import Script from 'next/script';
import './globals.css';

export const metadata: Metadata = {
  title: 'Mackafunk',
  description: 'Library, Jazz, Funk, Soul, Psychedelic, Rock, Disco, Boogie, Hip Hop, Dub, Cumbia music',
  icons: {
    icon: '/favicon.png',
    shortcut: '/favicon.png',
    apple: '/apple-touch-icon.png',
  },
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
      <body>
        {children}
        <Script
          defer
          src="https://a.agtc.app/script.js"
          data-website-id="2ed6bf8e-1312-4fb5-ad9d-b74cc5c97744"
          data-domains="macka.agtc.app"
          strategy="afterInteractive"
        />
      </body>
    </html>
  );
}
