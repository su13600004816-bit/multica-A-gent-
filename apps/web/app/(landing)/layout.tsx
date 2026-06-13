import localFont from "next/font/local";
import { LocaleProvider } from "@/features/landing/i18n";
import { getRequestLocale } from "@/lib/request-locale";

// Self-hosted (next/font/local) — hermetic build, no Google Fonts egress. See app/layout.tsx.
const instrumentSerif = localFont({
  src: "../fonts/InstrumentSerif.woff2",
  weight: "400",
  variable: "--font-serif",
  display: "swap",
});

const notoSerifSC = localFont({
  src: "../fonts/NotoSerifSC.woff2",
  weight: "400",
  variable: "--font-serif-zh",
  display: "swap",
});

const jsonLd = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      name: "Multica",
      url: "https://www.multica.ai",
      sameAs: ["https://github.com/multica-ai/multica"],
    },
    {
      "@type": "SoftwareApplication",
      name: "Multica",
      applicationCategory: "ProjectManagement",
      operatingSystem: "Web",
      description:
        "Open-source project management platform that turns coding agents into real teammates.",
      offers: {
        "@type": "Offer",
        price: "0",
        priceCurrency: "USD",
      },
    },
  ],
};

export default async function LandingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const initialLocale = await getRequestLocale();

  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <div className={`${instrumentSerif.variable} ${notoSerifSC.variable} landing-light h-full overflow-x-hidden overflow-y-auto bg-white`}>
        <LocaleProvider initialLocale={initialLocale}>{children}</LocaleProvider>
      </div>
    </>
  );
}
