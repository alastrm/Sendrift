export default function BrandMark({ className = "" }: { className?: string }) {
  return (
    <svg className={className} width="34" height="34" viewBox="0 0 34 34" fill="none" aria-hidden="true">
      <path d="M5 12.5h12c6 0 8-3.5 8-7" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" />
      <path d="M5 19h16c6 0 8-3.5 8-7" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" />
      <path d="M5 25.5h9c6 0 8-3.5 8-7" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" />
    </svg>
  );
}
