import { Configuration, RedirectRequest, BrowserCacheLocation } from "@azure/msal-browser";

export const msalConfig: Configuration = {
  auth: {
    clientId: "d14bccac-4a37-4919-89a3-24272a0825bc",
    authority: "https://bettersnap.ciamlogin.com/bettersnap.onmicrosoft.com",
    knownAuthorities: ["bettersnap.ciamlogin.com", "74e900cf-7b3a-4593-8207-deec6656d91d.ciamlogin.com"],
    redirectUri: "https://bettersnapai.cdfsandbox3.org",
    postLogoutRedirectUri: "/",
  },
  cache: {
    cacheLocation: BrowserCacheLocation.LocalStorage,
  },
};

export const loginRequest: RedirectRequest = {
  scopes: ["openid", "profile", "email"],
};

export const tokenRequest: RedirectRequest = {
  scopes: ["api://d14bccac-4a37-4919-89a3-24272a0825bc/access_as_user"],
};
