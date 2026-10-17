import Link from "next/link";
import BrandMark from "./components/BrandMark";
import UploadPanel from "./components/UploadPanel";

export default function HomePage() {
  return (
    <main className="sendrift-home">
      <a href="#share-panel" className="skip-link">Skip to file selection</a>
      <div className="application">
        <header className="application-header">
          <Link href="/" className="wordmark" aria-label="Sendrift home">
            <BrandMark />
            <span>Sendrift</span>
          </Link>
        </header>
        <div className="application-content">
          <section className="product-intro" aria-labelledby="page-heading">
            <p className="product-eyebrow">Temporary file sharing</p>
            <h1 id="page-heading">Share a file<br /> <span>with a link.</span></h1>
            <p className="product-description">
              Choose a file, set an expiration, and send the link.
            </p>
          </section>
          <UploadPanel />
        </div>
      </div>
    </main>
  );
}
