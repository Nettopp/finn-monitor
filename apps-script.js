// Finn.no monitor — Google Apps Script
//
// OPPSETT:
// 1. Gå til https://script.google.com → Nytt prosjekt
// 2. Lim inn denne koden
// 3. Klikk tannhjulet (Prosjektinnstillinger) → Skriptegenskaper → Legg til:
//      GITHUB_TOKEN  →  ditt GitHub Personal Access Token (trenger kun "repo"-scope)
// 4. Kjør checkFinnEmails() manuelt én gang for å gi Gmail-tillatelse
// 5. Sett opp tidsstyrt trigger:
//      Triggere (klokke-ikon) → + Legg til trigger
//      Funksjon: checkFinnEmails
//      Hendelseskilde: Tidsstyrt
//      Type: Minutttimer
//      Intervall: Hvert 5. minutt
//
// GITHUB TOKEN:
// Gå til https://github.com/settings/tokens → Generate new token (classic)
// Scope: kryss av "repo" (eller kun "workflow" hvis du vil begrense)
// Kopier token og lim inn i skriptegenskapene over.

const GITHUB_OWNER = "Nettopp";
const GITHUB_REPO  = "finn-monitor";
const FINN_SEARCH  = 'from:(finn.no) is:unread subject:(wingfoil OR "wing foil" OR vindvinge OR "wing surfer" OR foil OR surfebrett)';

function checkFinnEmails() {
  const threads = GmailApp.search(FINN_SEARCH, 0, 20);

  if (threads.length === 0) {
    return;
  }

  console.log(`Fant ${threads.length} uleste Finn.no-eposter — trigger GitHub`);

  // Mark as read immediately to prevent re-trigger before Python processes them
  threads.forEach(t => t.markRead());

  triggerGitHub();
}

function triggerGitHub() {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");

  if (!token) {
    console.error("GITHUB_TOKEN ikke satt i skriptegenskaper");
    return;
  }

  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/dispatches`;

  const response = UrlFetchApp.fetch(url, {
    method: "post",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github.v3+json",
      "Content-Type": "application/json",
    },
    payload: JSON.stringify({ event_type: "finn_email" }),
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();
  if (code === 204) {
    console.log("GitHub Actions trigget OK");
  } else {
    console.error(`GitHub API svarte ${code}: ${response.getContentText()}`);
  }
}
