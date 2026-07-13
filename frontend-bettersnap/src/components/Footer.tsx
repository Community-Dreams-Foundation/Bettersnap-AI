import { Link } from "react-router-dom";
import logo from "@/assets/logo.jpeg";

const Footer = () => {
  const linkClass =
    "text-sm text-muted-foreground hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded";

  return (
    <footer className="py-8 border-t border-border" role="contentinfo">
      <div className="container mx-auto px-4 flex flex-col md:flex-row items-center justify-between gap-4 text-center md:text-left">
        <Link to="/" className="flex items-center gap-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary rounded">
          <img src={logo} alt="BetterSnap AI" className="w-7 h-7 rounded-lg object-cover" />
          <span className="font-heading font-bold text-foreground">
            Better<span className="text-gradient">Snap</span> AI
          </span>
        </Link>

        <div className="flex flex-col md:flex-row items-center gap-3 md:gap-6">
          <nav aria-label="Legal" className="flex flex-wrap items-center justify-center gap-x-6 gap-y-2">
            <Link to="/privacy-policy" className={linkClass}>
              Privacy Policy
            </Link>
            <Link to="/terms" className={linkClass}>
              Terms &amp; Conditions
            </Link>
            <Link to="/contact-support" className={linkClass}>
              Contact Support
            </Link>
          </nav>
          <p className="text-sm text-muted-foreground">© 2026 BetterSnap AI. All rights reserved.</p>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
