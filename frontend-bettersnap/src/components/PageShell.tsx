import PremiumBackground from "./PremiumBackground";

interface PageShellProps {
  children: React.ReactNode;
  className?: string;
}

const PageShell = ({ children, className = "" }: PageShellProps) => {
  return (
    <div className="min-h-screen relative">
      <PremiumBackground />
      <div className={`relative z-10 ${className}`}>{children}</div>
    </div>
  );
};

export default PageShell;
