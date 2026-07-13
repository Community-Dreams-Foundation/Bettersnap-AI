import { Link, useLocation, useNavigate } from "react-router-dom";
import { Menu, X, ArrowRight, LogOut, Settings, CreditCard, User, ChevronDown } from "lucide-react";
import logo from "@/assets/logo.jpeg";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useCaseCategories } from "@/data/useCases";

const Navbar = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const isLanding = location.pathname === "/";
  const isUseCasePage = location.pathname.startsWith("/use-cases/");
  const isForCompanies = location.pathname === "/for-companies";
  const isOrgFlow = location.pathname.startsWith("/org/");
  const isMinimalNav = isForCompanies || isOrgFlow;
  // Public surfaces show landing-style nav (logo, How It Works, Features, Use Cases, Pricing, FAQ, Get Started)
  const isPublic = isLanding || isUseCasePage;
  const [mobileOpen, setMobileOpen] = useState(false);
  const { user, signOut } = useAuth();

  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const [useCasesOpen, setUseCasesOpen] = useState(false);
  const [mobileUseCasesOpen, setMobileUseCasesOpen] = useState(false);
  const useCasesRef = useRef<HTMLDivElement>(null);
  const useCasesCloseTimer = useRef<number | null>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setProfileOpen(false);
      }
      if (useCasesRef.current && !useCasesRef.current.contains(e.target as Node)) {
        setUseCasesOpen(false);
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setUseCasesOpen(false);
        setProfileOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKey);
    };
  }, []);

  const navLinks = [
    { to: "/dashboard", label: "Dashboard" },
    { to: "/generate", label: "Generate" },
    { to: "/history", label: "History" },
  ];

  const landingLinks: { label: string; href: string; to?: string }[] = [
    { label: "How It Works", href: "#how-it-works" },
    { label: "Features", href: "#features" },
    { label: "Use Cases", href: "#use-cases" },
    { label: "For Companies", href: "/for-companies", to: "/for-companies" },
    { label: "Pricing", href: "#pricing" },
    { label: "FAQ", href: "#faq" },
  ];

  const [activeSection, setActiveSection] = useState<string>("");
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    if (!isLanding) return;
    const ids = landingLinks
      .filter((l) => !l.to)
      .map((l) => l.href.slice(1));
    const onScroll = () => {
      const y = window.scrollY;
      setScrolled(y > 8);
      if (y < 96) {
        setActiveSection("");
        return;
      }
      const cutoff = y + 80;
      let currentId = "";
      let currentTop = -Infinity;
      ids.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        const top = el.getBoundingClientRect().top + window.scrollY;
        if (top <= cutoff && top > currentTop) {
          currentTop = top;
          currentId = id;
        }
      });
      setActiveSection(currentId);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLanding]);


  const handleSignOut = async () => {
    await signOut();
    navigate("/login");
  };

  // Smooth-scroll to a landing-page section. Works from /, navigates to /#id from other pages.
  const goToLandingSection = (id: string) => {
    if (isLanding) {
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      else window.location.hash = id;
    } else {
      navigate(`/#${id}`);
    }
  };

  // Hover handlers for the desktop Use Cases dropdown (with small close delay so the
  // pointer can cross the gap between trigger and menu without closing it).
  const openUseCases = () => {
    if (useCasesCloseTimer.current) {
      window.clearTimeout(useCasesCloseTimer.current);
      useCasesCloseTimer.current = null;
    }
    setUseCasesOpen(true);
  };
  const scheduleCloseUseCases = () => {
    if (useCasesCloseTimer.current) window.clearTimeout(useCasesCloseTimer.current);
    useCasesCloseTimer.current = window.setTimeout(() => setUseCasesOpen(false), 150);
  };

  return (
    <nav
      className="sticky top-0 left-0 right-0 z-50 px-0"
      role="navigation"
      aria-label="Main navigation"
    >
      <div
        className={`container mx-auto px-6 h-14 flex items-center justify-between bg-[hsl(var(--navbar))] rounded-2xl relative transition-shadow duration-300 ${
          scrolled ? "shadow-2xl shadow-primary/10" : "shadow-lg"
        }`}
      >
        <Link
          to="/"
          onClick={(e) => {
            if (isLanding) {
              e.preventDefault();
              window.scrollTo({ top: 0, behavior: "smooth" });
              setActiveSection("");
              if (window.location.hash) {
                window.history.replaceState(null, "", window.location.pathname);
              }
            }
          }}
          className="flex items-center gap-2.5"
          aria-label="BetterSnap AI Home"
        >
          <img src={logo} alt="BetterSnap AI" className="w-9 h-9 rounded-xl object-cover" />
          <span className="font-heading text-xl font-bold text-white">BetterSnap AI</span>
        </Link>

        {/* Desktop nav - centered landing links (shown on landing and on public use-case pages) */}
        {isPublic && (
          <div className="hidden lg:flex absolute left-1/2 -translate-x-1/2 items-center gap-1">
            {landingLinks.map((link) => {
              const sectionId = link.href.slice(1);
              if (link.label === "Use Cases") {
                return (
                  <div
                    key="use-cases"
                    className="relative"
                    ref={useCasesRef}
                    onMouseEnter={openUseCases}
                    onMouseLeave={scheduleCloseUseCases}
                  >
                    <button
                      type="button"
                      onClick={() => {
                        setUseCasesOpen(false);
                        goToLandingSection("use-cases");
                      }}
                      aria-haspopup="menu"
                      aria-expanded={useCasesOpen}
                      className={`inline-flex items-center text-sm font-medium px-3 py-2 rounded-lg transition-colors ${
                        useCasesOpen || activeSection === "use-cases"
                          ? "text-white bg-white/10"
                          : "text-white/70 hover:text-white hover:bg-white/5"
                      }`}
                    >
                      Use Cases
                    </button>
                    {useCasesOpen && (
                      <div
                        role="menu"
                        onMouseEnter={openUseCases}
                        onMouseLeave={scheduleCloseUseCases}
                        className="absolute left-1/2 -translate-x-1/2 mt-3 w-[640px] max-w-[92vw] bg-white rounded-2xl border border-border shadow-2xl p-3 z-50"
                      >
                        <div className="grid grid-cols-2 gap-1">
                          {useCaseCategories.map((uc) => {
                            const Icon = uc.icon;
                            return (
                              <Link
                                key={uc.slug}
                                to={`/use-cases/${uc.slug}`}
                                role="menuitem"
                                onClick={() => setUseCasesOpen(false)}
                                className="group flex items-start gap-3 p-3 rounded-xl hover:bg-primary/5 transition-colors"
                              >
                                <div className="w-9 h-9 rounded-lg bg-primary/10 group-hover:bg-primary/20 flex items-center justify-center shrink-0 transition-colors">
                                  <Icon className="w-4 h-4 text-primary" />
                                </div>
                                <div className="min-w-0">
                                  <div className="text-sm font-semibold text-foreground group-hover:text-primary transition-colors">
                                    {uc.title}
                                  </div>
                                  <div className="text-xs text-muted-foreground leading-snug">
                                    {uc.shortDesc}
                                  </div>
                                </div>
                              </Link>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                );
              }
              if (link.to) {
                const isActive = location.pathname === link.to;
                return (
                  <Link
                    key={link.to}
                    to={link.to}
                    className={`text-sm font-medium px-3 py-2 rounded-lg transition-colors ${
                      isActive ? "text-white bg-white/10" : "text-white/70 hover:text-white hover:bg-white/5"
                    }`}
                  >
                    {link.label}
                  </Link>
                );
              }
              const isActive = activeSection === sectionId;
              return (
                <button
                  key={link.href}
                  type="button"
                  onClick={() => goToLandingSection(sectionId)}
                  className={`text-sm font-medium px-3 py-2 rounded-lg transition-colors ${
                    isActive ? "text-white bg-white/10" : "text-white/70 hover:text-white hover:bg-white/5"
                  }`}
                >
                  {link.label}
                </button>
              );
            })}
          </div>
        )}

        {/* Desktop nav - right side */}
        <div className="hidden lg:flex items-center gap-1">
          {!isPublic && !isMinimalNav &&
            navLinks.map((link) => (
              <Link
                key={link.to}
                to={link.to}
                className={`text-sm font-medium px-3 py-2 rounded-lg transition-colors ${
                  location.pathname === link.to
                    ? "text-white bg-white/10"
                    : "text-white/70 hover:text-white hover:bg-white/5"
                }`}
              >
                {link.label}
              </Link>
            ))}
          {isForCompanies ? (
            <Link
              to="/org/onboarding"
              className="ml-3 inline-flex items-center gap-1.5 px-5 py-2 text-sm font-semibold rounded-full bg-white text-gray-900 hover:bg-gray-100 hover-scale transition-colors"
            >
              Create Your Team <ArrowRight className="w-3.5 h-3.5" aria-hidden="true" />
            </Link>
          ) : isOrgFlow ? null : user && !isPublic ? (
            <div className="relative ml-3" ref={profileRef}>
              <button
                onClick={() => setProfileOpen(!profileOpen)}
                className="w-9 h-9 rounded-full bg-white/10 border border-white/20 flex items-center justify-center hover:bg-white/20 transition-colors"
                aria-label="Profile menu"
              >
                <User className="w-4 h-4 text-white" />
              </button>
              {profileOpen && (
                <div className="absolute right-0 mt-2 w-48 bg-[hsl(var(--navbar))] rounded-2xl border border-white/10 shadow-xl py-2 z-50">
                  <Link
                    to="/settings"
                    onClick={() => setProfileOpen(false)}
                    className="flex items-center gap-2 px-4 py-2.5 text-sm text-white/70 hover:text-white hover:bg-white/10 transition-colors"
                  >
                    <Settings className="w-4 h-4" /> Settings
                  </Link>
                  <Link
                    to="/billing"
                    onClick={() => setProfileOpen(false)}
                    className="flex items-center gap-2 px-4 py-2.5 text-sm text-white/70 hover:text-white hover:bg-white/10 transition-colors"
                  >
                    <CreditCard className="w-4 h-4" /> Billing
                  </Link>
                  <div className="border-t border-white/10 my-1" />
                  <button
                    onClick={() => {
                      setProfileOpen(false);
                      handleSignOut();
                    }}
                    className="flex items-center gap-2 w-full px-4 py-2.5 text-sm text-white/70 hover:text-white hover:bg-white/10 transition-colors"
                  >
                    <LogOut className="w-4 h-4" /> Sign Out
                  </button>
                </div>
              )}
            </div>
          ) : (
            <Link
              to="/login"
              className="ml-3 inline-flex items-center gap-1.5 px-5 py-2 text-sm font-semibold rounded-full bg-white text-gray-900 hover:bg-gray-100 hover-scale transition-colors"
            >
              Try BetterSnap AI <ArrowRight className="w-3.5 h-3.5" aria-hidden="true" />
            </Link>
          )}
        </div>

        {/* Mobile - minimal CTA / hidden for /org/* (no hamburger) */}
        {isForCompanies ? (
          <Link
            to="/org/onboarding"
            className="lg:hidden inline-flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded-full bg-white text-gray-900 hover:bg-gray-100 transition-colors"
          >
            Create Your Team
          </Link>
        ) : isOrgFlow ? null : (
          <button
            type="button"
            className="lg:hidden p-2 rounded-lg text-white/80 hover:text-white"
            onClick={() => setMobileOpen(!mobileOpen)}
            aria-label={mobileOpen ? "Close menu" : "Open menu"}
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
          </button>
        )}
      </div>


      {mobileOpen && (
        <div className="lg:hidden bg-[hsl(var(--navbar))] rounded-2xl mt-2 border border-white/10 px-4 pb-4 pt-2">
          {isPublic &&
            landingLinks.map((link) => {
              const sectionId = link.href.slice(1);
              if (link.label === "Use Cases") {
                return (
                  <div key="use-cases-mobile" className="py-1">
                    <div className="flex items-center justify-between">
                      <button
                        type="button"
                        onClick={() => {
                          setMobileOpen(false);
                          setMobileUseCasesOpen(false);
                          goToLandingSection("use-cases");
                        }}
                        className="flex-1 text-left text-sm font-medium py-2.5 text-white/70 hover:text-white transition-colors"
                      >
                        Use Cases
                      </button>
                      <button
                        type="button"
                        onClick={() => setMobileUseCasesOpen((v) => !v)}
                        aria-expanded={mobileUseCasesOpen}
                        aria-label={mobileUseCasesOpen ? "Hide use case categories" : "Show use case categories"}
                        className="p-2 text-white/70 hover:text-white"
                      >
                        <ChevronDown
                          className={`w-4 h-4 transition-transform ${mobileUseCasesOpen ? "rotate-180" : ""}`}
                        />
                      </button>
                    </div>
                    {mobileUseCasesOpen && (
                      <div className="pl-3 border-l border-white/10 ml-1 mt-1 space-y-1">
                        {useCaseCategories.map((uc) => (
                          <Link
                            key={uc.slug}
                            to={`/use-cases/${uc.slug}`}
                            onClick={() => {
                              setMobileOpen(false);
                              setMobileUseCasesOpen(false);
                            }}
                            className="block text-sm py-2 text-white/70 hover:text-white transition-colors"
                          >
                            {uc.title}
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                );
              }
              if (link.to) {
                return (
                  <Link
                    key={link.to}
                    to={link.to}
                    onClick={() => setMobileOpen(false)}
                    className="block text-sm font-medium py-2.5 text-white/70 hover:text-white transition-colors"
                  >
                    {link.label}
                  </Link>
                );
              }
              return (
                <button
                  key={link.href}
                  type="button"
                  onClick={() => {
                    setMobileOpen(false);
                    goToLandingSection(sectionId);
                  }}
                  className="block w-full text-left text-sm font-medium py-2.5 text-white/70 hover:text-white transition-colors"
                >
                  {link.label}
                </button>
              );
            })}
          {!isPublic &&
            navLinks.map((link) => (
              <Link
                key={link.to}
                to={link.to}
                onClick={() => setMobileOpen(false)}
                className="block text-sm font-medium py-2.5 text-white/70 hover:text-white transition-colors"
              >
                {link.label}
              </Link>
            ))}
          {user && !isPublic ? (
            <div className="mt-2 border-t border-white/10 pt-2 space-y-1">
              <Link
                to="/settings"
                onClick={() => setMobileOpen(false)}
                className="flex items-center gap-2 px-3 py-2.5 text-sm text-white/70 hover:text-white transition-colors"
              >
                <Settings className="w-4 h-4" /> Settings
              </Link>
              <Link
                to="/billing"
                onClick={() => setMobileOpen(false)}
                className="flex items-center gap-2 px-3 py-2.5 text-sm text-white/70 hover:text-white transition-colors"
              >
                <CreditCard className="w-4 h-4" /> Billing
              </Link>
              <button
                onClick={() => {
                  setMobileOpen(false);
                  handleSignOut();
                }}
                className="flex items-center gap-2 w-full px-3 py-2.5 text-sm text-white/70 hover:text-white transition-colors"
              >
                <LogOut className="w-4 h-4" /> Sign Out
              </button>
            </div>
          ) : (
            <Link
              to="/login"
              onClick={() => setMobileOpen(false)}
              className="block mt-2 px-5 py-2.5 text-sm font-semibold rounded-full bg-white text-gray-900 text-center"
            >
              Try BetterSnap AI
            </Link>
          )}
        </div>
      )}
    </nav>
  );
};

export default Navbar;
