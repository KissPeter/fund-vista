import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Shield, X } from "lucide-react";

/**
 * Privacy notice banner (REF-002 Phase 5).
 *
 * Pure move from Index: owns its dismissed flag (localStorage) so the
 * page only renders it.
 */
export function PrivacyBanner() {
  const privacyBannerKey = "privacy_banner_dismissed";
  const [showPrivacyBanner, setShowPrivacyBanner] = useState(true);

  useEffect(() => {
    setShowPrivacyBanner(localStorage.getItem(privacyBannerKey) !== "1");
  }, []);

  const dismissPrivacyBanner = (): void => {
    localStorage.setItem(privacyBannerKey, "1");
    setShowPrivacyBanner(false);
  };

  if (!showPrivacyBanner) {
    return null;
  }

  return (
    <Alert className="mb-6 border-primary/30 bg-primary/5 text-primary">
      <Shield className="h-4 w-4" />
      <AlertDescription className="pr-8">
        All data is saved only in your browser. Nobody else can read it.
      </AlertDescription>
      <Button
        variant="ghost"
        size="icon"
        onClick={dismissPrivacyBanner}
        className="absolute right-2 top-2 h-7 w-7 text-primary/80 hover:text-primary"
      >
        <X className="h-4 w-4" />
      </Button>
    </Alert>
  );
}
