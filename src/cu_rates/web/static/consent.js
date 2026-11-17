// Basic Consent Mode v2. This same-origin file is the only tag entry point.
(() => {
  "use strict";
  window.dataLayer = window.dataLayer || [];
  window.gtag = function () {
    window.dataLayer.push(arguments);
  };
  window.gtag("consent", "default", {
    analytics_storage: "denied",
    ad_storage: "denied",
    ad_user_data: "denied",
    ad_personalization: "denied",
  });

  const settings = document.querySelector("script[data-measurement-id]");
  // The server supplies the validated id and the explicit saved choice.
  if (settings.dataset.consent !== "granted") return;
  const id = settings.dataset.measurementId;
  window.gtag("consent", "update", { analytics_storage: "granted" });
  window.gtag("js", new Date());
  window.gtag("config", id, {
    allow_google_signals: false,
    allow_ad_personalization_signals: false,
    cookie_domain: "none",
    cookie_path: "/",
    cookie_flags: "Secure;SameSite=Strict",
    page_location: window.location.origin + window.location.pathname,
    page_referrer: "",
  });
  const tag = document.createElement("script");
  tag.async = true;
  tag.src = "https://www.googletagmanager.com/gtag/js?id=" + id;
  document.head.appendChild(tag);
})();
