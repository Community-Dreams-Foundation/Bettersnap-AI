import PageShell from "@/components/PageShell";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/hooks/use-toast";

const USAGE_ROWS = [
  { date: "Jun 15, 2026", action: "AI Headshot Generation", credits: 2 },
  { date: "Jun 10, 2026", action: "Profile Optimizer", credits: 1 },
  { date: "May 28, 2026", action: "AI Headshot Generation", credits: 2 },
];

const Billing = () => {
  const creditsUsed = 3;
  const creditsTotal = 10;
  const progressValue = (creditsUsed / creditsTotal) * 100;

  const handleUpgrade = () => {
    toast({
      title: "Coming soon",
      description: "Pro plans will be available shortly.",
    });
  };

  const handleAddPayment = () => {
    toast({
      title: "Coming soon",
      description: "Payment method setup will be available shortly.",
    });
  };

  return (
    <PageShell>
      <Navbar />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 pt-28 pb-20">
        <header className="mb-10">
          <h1 className="text-4xl font-bold text-foreground">Billing</h1>
          <p className="text-muted-foreground mt-2">Manage your plan and usage</p>
        </header>

        <div className="space-y-6">
          {/* Current Plan */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Current Plan</h2>
            <div className="flex items-center gap-3 mb-3">
              <span className="text-foreground text-lg font-medium">Free</span>
              <Badge variant="secondary">Current</Badge>
            </div>
            <p className="text-muted-foreground mb-5">
              Includes 10 AI headshot credits per month, basic styles, and standard resolution downloads.
            </p>
            <Button onClick={handleUpgrade} className="gradient-cta text-white">
              Upgrade to Pro
            </Button>
          </section>

          {/* Credits Remaining */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Credits Remaining</h2>
            <div className="flex items-center justify-between mb-2">
              <span className="text-foreground font-medium">
                {creditsUsed} of {creditsTotal} used
              </span>
              <span className="text-muted-foreground text-sm">{creditsTotal - creditsUsed} left</span>
            </div>
            <Progress value={progressValue} className="h-3" />
            <p className="text-sm text-muted-foreground mt-3">Credits reset monthly on the 1st of each month.</p>
          </section>

          {/* Usage History */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Usage History</h2>
            <div className="rounded-lg border border-border overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Date</TableHead>
                    <TableHead>Action</TableHead>
                    <TableHead className="text-right">Credits Used</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {USAGE_ROWS.map((row, idx) => (
                    <TableRow key={idx}>
                      <TableCell className="text-foreground">{row.date}</TableCell>
                      <TableCell className="text-foreground">{row.action}</TableCell>
                      <TableCell className="text-right text-foreground">{row.credits}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </section>

          {/* Payment Method */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Payment Method</h2>
            <p className="text-muted-foreground mb-4">No payment method added</p>
            <Button variant="outline" onClick={handleAddPayment}>
              Add Payment Method
            </Button>
          </section>
        </div>
      </main>
    </PageShell>
  );
};

export default Billing;
