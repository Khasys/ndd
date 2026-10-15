const LABELS = {
  available: "Disponible",
  pending_delete: "Suppression imminente",
  redemption: "En rédemption",
  expired: "Expiré",
  expiring: "Expire bientôt",
  active: "Actif",
  error: "Erreur de check",
};

const URGENCY = ["available", "pending_delete", "redemption", "expired", "expiring", "active"];

// Clés normalisées : RDAP écrit « client hold », EPP écrit « clientHold ».
const EPP = {
  active: "Aucune restriction ni opération en attente. Le domaine fonctionne normalement.",
  ok: "Aucune restriction ni opération en attente. Le domaine fonctionne normalement.",
  inactive: "Aucun serveur DNS délégué : le domaine ne résout pas.",
  addperiod: "Période de grâce suivant la création. Une suppression maintenant libère le nom sans rédemption.",
  autorenewperiod: "Période de grâce après l'expiration. Le registre a renouvelé d'office, le titulaire peut encore régler le renouvellement auprès du registrar.",
  renewperiod: "Période de grâce suivant un renouvellement explicite.",
  transferperiod: "Période de grâce suivant un transfert de registrar.",
  redemptionperiod: "Le registrar a demandé la suppression. Le titulaire peut encore restaurer le nom, avec frais de restauration.",
  pendingrestore: "Restauration demandée pendant la rédemption, en attente de validation par le registre.",
  pendingdelete: "Suppression en cours. Plus aucune restauration possible, le nom sera libéré à la fin de ce délai.",
  pendingcreate: "Création demandée, en attente de traitement par le registre.",
  pendingrenew: "Renouvellement demandé, en attente de traitement par le registre.",
  pendingtransfer: "Transfert vers un autre registrar en cours.",
  pendingupdate: "Modification demandée, en attente de traitement par le registre.",
  clientdeleteprohibited: "Suppression bloquée par le registrar (verrou de protection courant).",
  clienthold: "Domaine retiré du DNS par le registrar, souvent pour impayé ou litige. Il ne résout plus.",
  clientrenewprohibited: "Renouvellement bloqué par le registrar.",
  clienttransferprohibited: "Transfert bloqué par le registrar (verrou de protection courant).",
  clientupdateprohibited: "Modifications bloquées par le registrar.",
  serverdeleteprohibited: "Suppression bloquée par le registre.",
  serverhold: "Domaine retiré du DNS par le registre. Il ne résout plus.",
  serverrenewprohibited: "Renouvellement bloqué par le registre.",
  servertransferprohibited: "Transfert bloqué par le registre.",
  serverupdateprohibited: "Modifications bloquées par le registre.",
  serverrecoverprohibited: "Restauration bloquée par le registre (statut propre à l'Afnic).",
  clientrecoverprohibited: "Restauration bloquée par le registrar (statut propre à l'Afnic).",
  deleteprohibited: "Suppression bloquée.",
  renewprohibited: "Renouvellement bloqué.",
  transferprohibited: "Transfert bloqué.",
  updateprohibited: "Modifications bloquées.",
  locked: "Objet verrouillé : modifications impossibles.",
  associated: "Objet lié à un autre objet du registre.",
  obscured: "Certaines données sont masquées dans la réponse.",
  private: "Données non publiques.",
  proxy: "Le titulaire est représenté par un service d'anonymisation.",
  removed: "Certaines données ont été retirées de la réponse.",
  validated: "Données validées par le registre.",
};

const FIELDS = { state: "État", registrar: "Registrar", nameservers: "DNS", expires_at: "Expiration" };

const DAY = 86400000;
const dateFmt = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "2-digit", year: "numeric" });
const longFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "long", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });
const stampFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const exactFmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "full", timeStyle: "short" });

function frequency(seconds) {
  if (seconds === 86400) return "1 fois par jour";
  if (seconds % 86400 === 0) return `tous les ${seconds / 86400} jours`;
  if (seconds === 3600) return "toutes les heures";
  if (seconds % 3600 === 0) return `toutes les ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `toutes les ${seconds / 60} min`;
  return `toutes les ${seconds} s`;
}

document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    domains: [],
    meta: null,
    loaded: false,
    loadError: null,
    newName: "",
    adding: false,
    notice: null,
    filter: null,
    sortKey: "urgency",
    sortDir: 1,
    openId: null,
    history: {},
    busy: {},
    bulkRunning: false,
    sendingTest: false,
    legendOpen: false,
    settingsOpen: false,
    dropsOpen: false,
    dropsData: null,
    exploreOpen: false,
    exData: null,
    exError: "",
    exItems: [],
    exLimit: 50,
    exLoading: false,
    exCard: null,
    exMap: null,
    exBusy: {},
    exSeo: { running: false, done: 0, total: 0, key_set: true },
    seoKey: "",
    ex: { tld: "", q: "", status: "available", cat: "", len: "", sort: "score", nat: "", rank: "", taken: "", min: "", junk: "", seo: "" },
    exFiltersOpen: false,
    fiche: null,
    aiToken: "",
    aiStatus: null,
    domainFiche: null,
    df: { nat: "", len: "", rank: "", min: 0, junk: false },
    exOpen: null,
    exSelected: [],
    exBulkRunning: false,
    exBulkDone: 0,
    exViews: [],
    exLengths: [{ v: "", label: "Toutes" }, { v: "-3", label: "1 à 3" }, { v: "1", label: "1" }, { v: "2", label: "2" }, { v: "3", label: "3" }, { v: "4", label: "4" }, { v: "5", label: "5 et plus" }],
    exRanks: [{ v: "", label: "Toutes" }, { v: "100", label: "Top 100" }, { v: "500", label: "Top 500" }, { v: "1000", label: "Top 1 000" }, { v: "5000", label: "Top 5 000" }],
    orderUrl: "",
    dropTld: "",
    dropKinds: [],
    dropSort: "release",
    scansOpen: false,
    czdsUser: "",
    czdsPass: "",
    savingCzds: false,
    checkingVersion: false,
    now: Date.now(),
    labels: LABELS,
    query: "",
    tagFilter: null,
    abandonedOnly: false,
    busySignals: {},
    cfg: null,
    tokenInput: "",
    editingToken: false,
    savingToken: false,
    pairing: null,
    themes: [{ key: "system", label: "Système" }, { key: "light", label: "Clair" }, { key: "dark", label: "Sombre" }],
    days: ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],

    init() {
      this.restoreFilters();
      for (const key of ["query", "filter", "tagFilter", "abandonedOnly", "sortKey", "sortDir"]) {
        this.$watch(key, () => this.saveFilters());
      }
      this.load();
      // Sur téléphone, le focus automatique ouvrirait le clavier à chaque lancement.
      if (!this.mobile) this.$nextTick(() => this.$refs.input.focus());
      setInterval(() => (this.now = Date.now()), 30000);
      setInterval(() => this.load(true), 60000);
      // Appli installée : retour au premier plan après une pause, données à rafraîchir.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") { this.now = Date.now(); this.load(true); }
      });
      this.$watch("legendOpen", () => this.lockScroll());
      this.$watch("settingsOpen", () => this.lockScroll());
      this.$watch("dropsOpen", () => this.lockScroll());
      this.$watch("exploreOpen", () => this.lockScroll());
      // Suppressions ouvert : le travail .fr est suivi en direct.
      setInterval(() => { if (this.dropsOpen && document.visibilityState === "visible") this.loadDrops(); }, 15000);
      this.$watch("openId", (id) => {
        this.lockScroll();
        // Panneau d'un domaine surveillé : même bloc « Valeur » que la fiche des autres écrans.
        const d = this.domains.find((x) => x.id === id);
        if (d) this.remember();
        this.domainFiche = null;
        if (d) this.api("GET", `/api/explore/fiche?name=${encodeURIComponent(d.name)}`).then((f) => (this.domainFiche = f)).catch(() => {});
      });
      this.$watch("fiche", (f) => {
        this.lockScroll();
        // Fiche ouverte : le clavier va dessus, pas sur la liste derrière.
        if (f) this.$nextTick(() => document.querySelector(".fiche-close")?.focus({ preventScroll: true }));
      });
      window.addEventListener("popstate", () => this.back());
      if ("serviceWorker" in navigator && window.isSecureContext) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
    },

    get mobile() { return window.matchMedia("(max-width: 640px)").matches; },

    lockScroll() {
      // La fiche est par-dessus tout : la page derrière ne défile plus, sur ordinateur aussi.
      document.body.classList.toggle("sheet-open", (this.mobile && (this.paged || this.openId !== null)) || this.fiche !== null);
    },

    get selected() {
      return this.openId === null ? null : this.domains.find((d) => d.id === this.openId) || null;
    },

    // Le panneau garde le dernier domaine affiché le temps de se fermer : ses liaisons
    // ne doivent jamais lire un domaine nul pendant le démontage.
    get shown() {
      const current = this.selected;
      if (current) this._lastShown = current;
      return current || this._lastShown;
    },

    escape() {
      if (this.fiche) this.fiche = null;
      else if (this.exploreOpen && this.exFiltersOpen) this.exFiltersOpen = false;
      else if (this.openId !== null) this.openId = null;
      else if (this.exploreOpen && this.ex.q) { this.ex.q = ""; this.exTyped(); }
      else if (this.paged) this.showList();
    },

    get paged() { return this.legendOpen || this.settingsOpen || this.dropsOpen || this.exploreOpen; },

    showList() {
      this.legendOpen = false;
      this.settingsOpen = false;
      this.dropsOpen = false;
      this.exploreOpen = false;
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    home() {
      this.openId = null;
      this.fiche = null;
      this.showList();
    },

    // Retour arrière du navigateur ou du téléphone : ferme ce qui est ouvert au lieu de quitter l'appli.
    remember() {
      try { history.pushState({ dw: true }, ""); } catch (_) {}
    },

    back() {
      if (this.fiche) this.fiche = null;
      else if (this.openId !== null) this.openId = null;
      else if (this.paged) this.showList();
    },

    async openSheet(name) {
      this.openId = null;
      this.fiche = null;
      // L'écran déjà affiché le reste : un second appui ne le ferme pas.
      if (this[`${name}Open`]) return;
      this.remember();
      const opening = true;
      this.legendOpen = false;
      this.dropsOpen = false;
      this.exploreOpen = false;
      if (name === "legend") {
        this.settingsOpen = false;
        this.legendOpen = opening;
      } else if (name === "drops") {
        this.settingsOpen = false;
        this.dropsOpen = opening;
        if (opening) await this.loadDrops();
      } else if (name === "explore") {
        this.settingsOpen = false;
        this.exploreOpen = opening;
        if (opening) {
          this.exRestore();
          this.exSeoStatus();
          this.exLoadViews();
          this.loadOrderUrl();
          await Promise.all([this.exLoad(), this.exLoadMap()]);
        }
      } else {
        await this.toggleSettings();
      }
    },

    // Explorer : tous les noms suivis, extension par extension.

    // Fiche d'un nom : la même partout (Domaines, Suppressions, Explorer).

    loadOrderUrl() {
      if (!this.orderUrl) this.api("GET", "/api/settings").then((c) => (this.orderUrl = c.order_url)).catch(() => {});
    },

    async openFiche(name) {
      this.loadOrderUrl();
      if (!this.fiche) this.remember();
      try {
        this.fiche = await this.api("GET", `/api/explore/fiche?name=${encodeURIComponent(name)}`);
      } catch (e) {
        this.say(`${name} : ${e.message}`, "error");
      }
    },

    async ficheReload() {
      if (this.fiche) await this.openFiche(this.fiche.name);
    },

    async ficheCheck() {
      const item = await this.verifyName(this.fiche.name);
      if (!item) return;
      this.exReplace(item);
      if (this.dropsOpen) this.loadDrops();
      await this.ficheReload();
    },

    async ficheWatch() {
      this.newName = this.fiche.name;
      await this.add();
      this.exReplace({ name: this.fiche.name, watched: true });
      await this.ficheReload();
    },

    async ficheSeo() {
      await this.exSeoOne({ name: this.fiche.name });
      await this.ficheReload();
    },

    async ficheFlag() {
      const name = this.fiche.name;
      const reserved = this.fiche.item.status !== 7;
      await this.api("POST", "/api/reserved/flag", { name, reserved });
      this.say(reserved ? `${name} marqué réservé.` : `${name} remis dans la liste.`);
      await this.ficheReload();
      // Marqué : il quitte la liste des noms à saisir ; remis : sa ligne prend le nouvel état.
      if (reserved) this.exItems = this.exItems.filter((x) => x.name !== name);
      else if (this.fiche && this.fiche.item) this.exReplace(this.fiche.item);
    },

    openDomain(id) {
      this.fiche = null;
      this.showList();
      this.openId = id;
    },

    valueHtml(f) {
      const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      if (!f) return "";
      const rows = [];
      const item = f.item;
      const opinion = f.ai || (item && item.ai);
      if (opinion) {
        rows.push(["Avis IA", `<span class="ai-notes">Exploitation <b>${Number(opinion.exploit)}</b> · revente <b>${Number(opinion.resale)}</b></span>`]);
        rows.push(["Revente rapide", esc(this.priceRange(opinion.quick_low, opinion.quick_high))]);
        rows.push(["Utilisateur final", esc(this.priceRange(opinion.final_low, opinion.final_high))]);
        rows.push(["Commentaire", `<span class="ai-comment">${esc(opinion.comment)}</span>`]);
      }
      if (item && item.score !== undefined) {
        rows.push(["Score", `<span class="ex-bar fiche-bar"><i style="width:${Number(item.score)}%"></i><b>${Number(item.score)}</b></span>`]);
        rows.push(["Détail", esc(this.scoreTitle(item).replace(/^Score \d+ : /, "")) || "—"]);
        if (item.reasons && item.reasons.length) rows.push(["Raisons", item.reasons.map((r) => `<span class="ex-reason">${esc(r)}</span>`).join(" ")]);
      }
      const kinds = (this.exData && this.exData.kinds) || (this.dropsData && this.dropsData.kinds) || {};
      if (f.categories && f.categories.length) rows.push(["Catégories", esc(f.categories.map((c) => kinds[c] || c).join(", "))]);
      const natures = (this.exData && this.exData.natures) || (this.dropsData && this.dropsData.natures) || {};
      if (f.natures && f.natures.length) rows.push(["Nature", esc(f.natures.map((n) => natures[n] || n).join(", "))]);
      if (f.rank) rows.push(["Fréquence", `n° ${esc(this.fmt(f.rank))} de sa liste`]);
      if (item && item.created_on) rows.push(["Open data .fr", esc(`créé le ${this.shortDate(item.created_on)}` + (item.removed_on ? `, supprimé le ${this.shortDate(item.removed_on)}` : ""))]);
      if (item && item.release_at) rows.push(["Libération", esc("~" + this.shortDate(item.release_at))]);
      if (f.seo) {
        const parts = [];
        if (f.seo.pr) parts.push(`PageRank ${String(f.seo.pr).replace(".", ",")}`);
        if (f.seo.wb_first) parts.push(`archivé depuis ${f.seo.wb_first.slice(0, 4)}` + (f.seo.wb_years ? ` (${f.seo.wb_years} ans)` : ""));
        rows.push(["SEO", esc(parts.join(", ") || "aucun signal") + `, analysé ${esc(this.relative(f.seo.checked_at))}`]);
      }
      return rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
    },

    // Fourchette de prix d'un avis IA : « 2 000 à 6 000 € », « 0 € » quand rien.
    priceRange(low, high) {
      if (!high) return "0 €";
      const n = (v) => Number(v).toLocaleString("fr-FR");
      return low !== high ? `${n(low || 0)} à ${n(high)} €` : `${n(high)} €`;
    },

    // Étiquette courte dans les listes : verdict, puis revente rapide (sinon prix utilisateur final).
    aiTag(a) {
      if (!a) return "";
      const k = (v) => (v >= 1000 ? `${Math.round(v / 1000)} k€` : `${v} €`);
      return `${this.aiVerdict(a)} · ` + (a.quick_high ? `rapide ${k(a.quick_high)}` : `final ${k(a.final_high)}`);
    },

    aiVerdict(a) { return a && a.comment ? a.comment.split(".")[0] : ""; },

    aiClass(a) { return { "À prendre": "take", "À surveiller": "watch", "À laisser": "skip" }[this.aiVerdict(a)] || ""; },

    async newAiToken() {
      if (this.cfg && this.cfg.ai && this.cfg.ai.token_set && !confirm("Un nouveau jeton remplace l'ancien : la tâche de notation devra utiliser le nouveau. Continuer ?")) return;
      try {
        this.aiToken = (await this.api("POST", "/api/settings/ai-token")).token;
        await this.loadAiStatus();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async loadAiStatus() {
      try { this.aiStatus = await this.api("GET", "/api/ai/status"); } catch (_) {}
    },

    rdapDl(r) {
      if (!r) return "<dt>RDAP</dt><dd>Pas encore lu : « Vérifier ».</dd>";
      return this.rdapHtml(r);
    },

    exQuery(cursor) {
      const e = this.ex;
      const params = new URLSearchParams({ q: e.q.trim(), mode: "prefix", status: e.status, cat: e.cat, len: e.len, nat: e.nat, rank: e.rank, taken: e.taken, min: e.min && e.min !== "0" ? e.min : "", junk: e.junk, seo: e.seo, cursor: String(cursor) });
      if (this.ex.tld) params.set("tld", this.ex.tld);
      return params.toString();
    },

    async exLoad(reset = true) {
      if (reset) this.exLimit = 50;
      const ticket = (this._exTicket = (this._exTicket || 0) + 1);
      this.exLoading = true;
      try {
        const data = await this.api("GET", `/api/explore/rank?${this.exQuery(-1)}&limit=${this.exLimit}&sort=${this.ex.sort}`);
        // Une frappe ou un filtre plus récent a déjà relancé le chargement : ce résultat est périmé.
        if (ticket !== this._exTicket) return;
        this.exError = "";
        this.exData = data;
        this.ex.tld = data.tld;
        this.exItems = data.items;
      } catch (e) {
        if (ticket !== this._exTicket) return;
        // Extension mémorisée qui n'existe plus : retour à l'extension par défaut, une fois.
        if (e.status === 404 && this.ex.tld) {
          this.ex.tld = "";
          this.ex.taken = "";
          this.exLoading = false;
          return this.exLoad(reset);
        }
        this.exError = e.message;
      } finally {
        if (ticket === this._exTicket) this.exLoading = false;
      }
    },

    async exLoadMap() {
      try {
        this.exMap = await this.api("GET", "/api/explore/map");
      } catch (_) {
        this.exMap = null;
      }
    },

    async exTyped() {
      const q = this.ex.q.trim();
      const loading = this.exLoad();
      if (!q) { this.exCard = null; return loading; }
      try {
        const card = await this.api("GET", `/api/explore/name?label=${encodeURIComponent(q)}`);
        if (this.ex.q.trim() === q) {
          this.exCard = card;
          this.$nextTick(() => document.querySelector(".ex-card")?.scrollIntoView({ behavior: "smooth", block: "start" }));
        }
      } catch (_) {
        this.exCard = null;
      }
      return loading;
    },

    async exMapPick(key, tld) {
      const f = this.exMap.filters[key];
      await this.exSet({ tld, cat: f.cat || "", len: f.len ? String(f.len) : "", status: "available" });
      this.$nextTick(() => document.querySelector(".ex-status")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    },

    // Filtres : un seul point d'entrée, mémorisé pour la prochaine ouverture.
    async exSet(patch) {
      Object.assign(this.ex, patch);
      if ("cat" in patch && patch.cat && !["word", "form"].includes(patch.cat)) this.ex.nat = "";
      // Mélanges et tirets ne sont jamais « prononçables » : sans les suites incluses, la liste serait vide.
      if (patch.cat === "mixed") this.ex.junk = "1";
      this.exSelected = [];
      this.exOpen = null;
      try { localStorage.setItem("explorer", JSON.stringify({ ...this.ex, q: "" })); } catch (_) {}
      await this.exLoad();
    },

    exRestore() {
      try {
        const saved = JSON.parse(localStorage.getItem("explorer") || "null");
        if (saved) Object.assign(this.ex, saved, { q: "" });
      } catch (_) {}
    },

    exActive() {
      if (!this.exData) return [];
      const e = this.ex;
      const out = [];
      if (e.cat) out.push({ key: "cat", label: this.exData.kinds[e.cat] });
      if (e.nat) out.push({ key: "nat", label: this.exData.natures[e.nat] });
      if (e.len) out.push({ key: "len", label: this.exLengths.find((o) => o.v === e.len)?.label + " car." });
      if (e.rank) out.push({ key: "rank", label: this.exRanks.find((o) => o.v === e.rank)?.label });
      if (e.taken) out.push({ key: "taken", label: e.taken === "any" ? "Pris ailleurs" : "Pris en ." + e.taken });
      if (e.min && e.min !== "0") out.push({ key: "min", label: "Score ≥ " + e.min });
      if (e.junk) out.push({ key: "junk", label: "Suites et mélanges inclus" });
      if (e.seo) out.push({ key: "seo", label: this.exData.seo_filters[e.seo] });
      return out;
    },

    exClear(key) { return this.exSet({ [key]: "" }); },

    exReset() { return this.exSet({ cat: "", nat: "", len: "", rank: "", taken: "", min: "", junk: "", seo: "", status: "available" }); },

    async exLoadViews() {
      try { this.exViews = await this.api("GET", "/api/explore/views"); } catch (_) {}
    },

    async exSaveViews(views) {
      try { this.exViews = await this.api("PUT", "/api/explore/views", { views }); } catch (e) { this.say(e.message, "error"); }
    },

    async exSaveView() {
      const name = prompt("Nom de la vue", this.exActive().map((a) => a.label).join(", "));
      if (!name) return;
      const { q, ...filters } = this.ex;
      await this.exSaveViews([...this.exViews.filter((v) => v.name !== name), { name, filters }]);
    },

    exApplyView(v) { return this.exSet({ ...v.filters, q: "" }); },

    // Vue active : tous ses filtres sont ceux du moment.
    exViewOn(v) {
      return Object.entries(v.filters).every(([k, val]) => k === "q" || String(this.ex[k] ?? "") === String(val ?? ""));
    },

    async exDeleteView(n) {
      const v = this.exViews[n];
      if (!v || !confirm(`Supprimer la vue « ${v.name} » ? Les filtres affichés restent en place.`)) return;
      await this.exSaveViews(this.exViews.filter((_, i) => i !== n));
      this.say(`Vue « ${v.name} » supprimée.`);
    },

    exTlds() { return (this.ex.tld || "").split(",").filter(Boolean); },

    // Plusieurs extensions à la fois ; il en reste toujours une.
    exToggleTld(t) {
      const on = this.exTlds();
      const next = on.includes(t) ? on.filter((x) => x !== t) : [...on, t];
      if (!next.length) return;
      const order = this.exData ? this.exData.tlds : next;
      const patch = { tld: order.filter((x) => next.includes(x)).join(",") };
      // « Pris en .com » n'a pas de sens si .com est dans la sélection.
      if (next.includes(this.ex.taken)) patch.taken = "";
      return this.exSet(patch);
    },

    exToggle(name) {
      this.exSelected = this.exSelected.includes(name) ? this.exSelected.filter((x) => x !== name) : [...this.exSelected, name];
    },

    exSelectAll() {
      this.exSelected = this.exSelected.length ? [] : this.exItems.map((i) => i.name);
    },

    async exBulk(action) {
      const names = [...this.exSelected];
      const items = this.exItems.filter((i) => names.includes(i.name));
      this.exBulkRunning = true;
      this.exBulkDone = 0;
      try {
        if (action === "seo") {
          await this.api("POST", "/api/explore/seo", { names: names.slice(0, 100) });
          this.say(`Analyse SEO de ${Math.min(names.length, 100)} noms lancée.`);
          const poll = setInterval(async () => {
            await this.exSeoStatus();
            if (!this.exSeo.running) { clearInterval(poll); await this.exLoad(false); }
          }, 3000);
          return;
        }
        for (const i of items) {
          if (action === "check") await this.exCheck(i);
          else if (action === "watch" && !i.watched) await this.exWatch(i);
          else if (action === "reserve") await this.api("POST", "/api/reserved/flag", { name: i.name, reserved: true });
          this.exBulkDone += 1;
        }
        if (action === "reserve") { await this.exLoad(false); this.exLoadMap(); }
        this.exSelected = [];
      } catch (e) {
        this.say(e.message, "error");
      } finally {
        this.exBulkRunning = false;
      }
    },

    scoreTitle(i) {
      if (!i.parts) return "";
      const names = { shape: "forme", popularity: "popularité", demand: "demande", history: "historique", seo: "SEO" };
      return `Score ${i.score} : ` + Object.entries(i.parts).filter(([, v]) => v).map(([k, v]) => `${names[k]} ${v}`).join(", ");
    },

    rdapHtml(r) {
      const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      const rows = [[r.source === "whois" ? "WHOIS" : "RDAP", `${r.available ? "libre" : "enregistré"}, lu ${this.relative(r.at)}`]];
      if (r.registrar) rows.push(["Registrar", r.registrar]);
      if (r.registered_at) rows.push(["Créé", this.shortDate(r.registered_at)]);
      if (r.updated_at) rows.push(["Modifié", this.shortDate(r.updated_at)]);
      if (r.expires_at) rows.push(["Expire", this.shortDate(r.expires_at)]);
      if (r.statuses?.length) rows.push(["Statuts", r.statuses.join(", ")]);
      if (r.nameservers?.length) rows.push(["DNS", r.nameservers.join(", ")]);
      return rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    },

    orderLink(name) {
      return (this.orderUrl || "https://shop.gandi.net/fr/domain/suggest?search={domain}").replace("{domain}", encodeURIComponent(name));
    },

    get registrarName() {
      let host = "";
      try { host = new URL(this.orderUrl || "https://shop.gandi.net/").hostname; } catch (e) { return ""; }
      if (host.includes("gandi")) return "Gandi";
      if (host.includes("ovh")) return "OVH";
      return host.replace(/^(www|shop)\./, "");
    },

    exAvailable(i) { return [2, 3, 4, 5].includes(i.status); },

    scoreClass(n) { return n >= 70 ? "hi" : n >= 45 ? "mid" : "lo"; },

    async exFlag(i, isReserved) {
      try {
        await this.api("POST", "/api/reserved/flag", { name: i.name, reserved: isReserved });
        this.exItems = this.exItems.filter((x) => x.name !== i.name);
        this.say(isReserved ? `${i.name} marqué réservé.` : `${i.name} remis dans les noms à saisir.`);
        this.exLoadMap();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    reservedLine(r) {
      const c = r.counts || {};
      const when = r.at ? `, mis à jour ${this.relative(r.at)}` : ", pas encore téléchargées";
      return `${this.fmt(c.afnic)} termes Afnic, ${this.fmt(c.commune)} formes de noms de communes, ${this.fmt(r.manual_reserved)} marqués à la main, ${this.fmt(r.manual_ok)} remis${when}.`;
    },

    async saveWeights() {
      try {
        this.cfg.weights = await this.api("PUT", "/api/settings/weights", { values: this.cfg.weights.values });
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async saveOrderUrl() {
      try {
        const res = await this.api("PUT", "/api/settings/order-url", { url: this.cfg.order_url || "" });
        this.cfg.order_url = this.orderUrl = res.order_url;
        this.say("Lien de réservation enregistré.");
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async saveReservedMode(mode) {
      try {
        this.cfg.reserved = await this.api("PUT", "/api/settings/reserved", { mode });
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async refreshReserved() {
      try {
        await this.api("POST", "/api/reserved/refresh");
        this.cfg.reserved.running = true;
        const poll = setInterval(async () => {
          await this.loadSettings();
          if (!this.cfg.reserved.running) clearInterval(poll);
        }, 3000);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async exSeoStatus() {
      try { this.exSeo = await this.api("GET", "/api/explore/seo"); } catch (_) {}
    },

    async exSeoOne(i) {
      this.exBusy[i.name] = true;
      try {
        await this.api("POST", "/api/explore/seo", { names: [i.name] });
        await this.exLoad(false);
        if (this.exCard) await this.exTyped();
        this.say(`${i.name} : analyse SEO faite.`);
      } catch (e) {
        this.say(`${i.name} : ${e.message}`, "error");
      } finally {
        this.exBusy[i.name] = false;
      }
    },

    async exSeoTop() {
      const names = this.exItems.filter((i) => this.exAvailable(i)).slice(0, 50).map((i) => i.name);
      if (!names.length) return;
      try {
        const res = await this.api("POST", "/api/explore/seo", { names });
        this.exSeo = { ...this.exSeo, running: !res.done, done: 0, total: names.length };
        this.say(`Analyse SEO de ${names.length} noms lancée : environ une seconde par nom.`);
        const poll = setInterval(async () => {
          await this.exSeoStatus();
          if (!this.exSeo.running) { clearInterval(poll); await this.exLoad(false); this.say("Analyse SEO terminée."); }
        }, 3000);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async saveSeoKey() {
      try {
        this.cfg.seo = await this.api("PUT", "/api/settings/seo", { key: this.seoKey });
        this.seoKey = "";
        this.say(this.cfg.seo.key_set ? "Clé Open PageRank enregistrée." : "Clé Open PageRank retirée.");
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    exDetail(i) {
      const parts = [];
      if (i.created_on) parts.push(`créé le ${this.shortDate(i.created_on)}`);
      if (i.removed_on && i.status !== 1) parts.push(`supprimé le ${this.shortDate(i.removed_on)}`);
      if (i.expires_at && i.status === 1) parts.push(`échéance ${this.shortDate(i.expires_at)}`);
      if (i.release_at && i.status === 4) parts.push(`libération ~${this.shortDate(i.release_at)}`);
      if (i.checked_at) parts.push(`vérifié ${this.relative(i.checked_at)}`);
      if (i.next_check_at) parts.push(`prochaine vérification le ${this.shortDate(i.next_check_at)}`);
      return parts.join(", ");
    },

    exReplace(item) {
      for (const list of [this.exItems, this.exCard ? this.exCard.items : []]) {
        const at = list.findIndex((x) => x.name === item.name);
        if (at >= 0) list[at] = { ...list[at], ...item };
      }
    },

    async verifyName(name) {
      this.exBusy[name] = true;
      try {
        const item = await this.api("POST", "/api/explore/check", { name });
        this.say(`${name} : ${item.status_label || "vérifié"}.`);
        return item;
      } catch (e) {
        this.say(`${name} : ${e.message}`, "error");
        return null;
      } finally {
        this.exBusy[name] = false;
      }
    },

    async exCheck(i) {
      const item = await this.verifyName(i.name);
      if (item) { this.exReplace(item); this.exLoadMap(); }
    },

    async exWatch(i) {
      this.newName = i.name;
      await this.add();
      if (this.domains.some((x) => x.name === i.name)) this.exReplace({ name: i.name, watched: true });
    },

    async recheckDrop(d) {
      if (await this.verifyName(d.name)) await this.loadDrops();
    },

    fmt(n) { return (n || 0).toLocaleString("fr-FR"); },

    clock(v) { return v ? new Date(v).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : ""; },

    sourceLabel(s) { return { anniversaire: "date anniversaire", suppression: "suppression", manuel: "à la demande" }[s] || s; },

    frNow(f) {
      const w = f.work;
      if (f.paused_until && new Date(f.paused_until) > new Date()) return `en pause jusqu'au ${this.exact(f.paused_until)} : l'Afnic a signalé trop de requêtes.`;
      if (w.current) return `vérifie ${w.current}.`;
      if (w.due_now) return `${this.fmt(w.due_now)} vérification(s) en attente, ${f.rate} par minute : environ ${this.duration(w.due_now / f.rate * 60)} de file.`;
      return f.next_at ? `rien à faire. Prochaine vérification le ${this.exact(f.next_at)}.` : "rien à faire.";
    },

    frToday(f) {
      const w = f.work;
      const done = Object.values(w.done_today).reduce((a, b) => a + b, 0);
      const left = w.left_today.anniversaries + w.left_today.removals;
      const detail = Object.entries(w.done_today).map(([k, n]) => `${this.fmt(n)} ${this.sourceLabel(k)}`).join(", ");
      const rest = left ? `${this.fmt(left)} restante(s) (${this.fmt(w.left_today.anniversaries)} date(s) anniversaire, ${this.fmt(w.left_today.removals)} suppression(s))` : "plus rien de prévu";
      return `${this.fmt(done)} faite(s)${detail ? " (" + detail + ")" : ""}, ${rest}.`;
    },

    frTomorrow(f) {
      const t = f.work.tomorrow;
      const total = t.anniversaries + t.removals;
      return total ? `${this.fmt(total)} prévue(s) : ${this.fmt(t.anniversaries)} date(s) anniversaire, ${this.fmt(t.removals)} suppression(s).` : "rien de prévu.";
    },

    async saveDropsSettings() {
      try {
        this.cfg.drops = await this.api("PUT", "/api/settings/drops", { keep_days: this.cfg.drops.keep_days });
        this.say("Durée d'affichage enregistrée.");
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    // Suppressions : noms courts, mots et noms de famille sortis des zones ICANN.

    async loadDrops() {
      this.loadOrderUrl();
      try {
        this.dropsData = await this.api("GET", "/api/drops");
      } catch (e) {
        this.say(`Suppressions indisponibles : ${e.message}`, "error");
      }
    },

    dropMatches(d) {
      const f = this.df;
      const len = f.len ? Number(f.len) : 0;
      const label = d.name.slice(0, -(d.tld.length + 1));
      return (!this.dropTld || d.tld === this.dropTld)
        && (!this.dropKinds.length || d.categories.some((c) => this.dropKinds.includes(c)))
        && (!f.nat || (d.natures || []).includes(f.nat))
        && (!len || (len < 0 ? label.length <= -len : len >= 5 ? label.length >= 5 : label.length === len))
        && (!f.rank || (d.rank && d.rank <= Number(f.rank)))
        && (!f.min || d.score >= f.min)
        && (f.junk || d.good !== false);
    },

    get dropsReleased() {
      if (!this.dropsData) return [];
      return this.dropsData.items
        .filter((d) => d.state === "available" && this.dropMatches(d))
        .sort((a, b) => (b.available_since || "").localeCompare(a.available_since || ""));
    },

    get dropsComing() {
      if (!this.dropsData) return [];
      const rank = { pending_delete: 0, redemption: 1, expired: 2 };
      const list = this.dropsData.items.filter((d) => d.state in rank && this.dropMatches(d));
      if (this.dropSort === "name") return list.sort((a, b) => a.name.localeCompare(b.name));
      return list.sort((a, b) => (a.release_at || "9").localeCompare(b.release_at || "9") || rank[a.state] - rank[b.state]);
    },

    get dropsSuspended() {
      if (!this.dropsData) return 0;
      const counts = this.dropsData.suspended || {};
      return this.dropTld ? counts[this.dropTld] || 0 : Object.values(counts).reduce((a, b) => a + b, 0);
    },

    get dropTlds() {
      return this.dropsData ? [...new Set(this.dropsData.items.map((d) => d.tld))].sort() : [];
    },

    toggleDropKind(k) {
      this.dropKinds = this.dropKinds.includes(k) ? this.dropKinds.filter((x) => x !== k) : [...this.dropKinds, k];
    },

    zoneLine(z) {
      if (!z.at) return "jamais lue";
      if (z.error) return `erreur : ${z.error}`;
      const when = this.relative(z.at);
      const names = (z.names || 0).toLocaleString("fr-FR");
      const took = z.seconds >= 90 ? `${Math.round(z.seconds / 60)} min` : `${z.seconds} s`;
      if (z.first) return `${names} noms suivis présents, référence posée ${when}. Premières comparaisons au prochain passage.`;
      return `${names} noms suivis présents, ${z.removed} sortis, ${z.added} entrés, lue ${when} en ${took}`;
    },

    duration(seconds) {
      if (seconds === null || seconds === undefined) return "";
      if (seconds >= 5400) return `${(seconds / 3600).toFixed(1).replace(".", ",").replace(",0", "")} h`;
      return seconds >= 90 ? `${Math.round(seconds / 60)} min` : `${Math.round(seconds)} s`;
    },

    speed(h) {
      return h.seconds > 0 && h.megabytes ? `${(h.megabytes / h.seconds).toFixed(1)} Mo/s` : "";
    },

    scanStamp(v) {
      return v ? stampFmt.format(new Date(v)) : "";
    },

    frLine(f) {
      if (f.error) return `erreur : ${f.error}`;
      if (!f.at) return "pas encore importé : premier import dans quelques minutes.";
      const names = (f.total || 0).toLocaleString("fr-FR");
      const registered = (f.registered || 0).toLocaleString("fr-FR");
      const week = (f.week || 0).toLocaleString("fr-FR");
      const undated = f.registered && !f.scheduled
        ? ` Attention : aucune date de création lue, aucune vérification planifiée. Colonnes lues : ${Object.values(f.columns || {}).join(", ") || "aucune"}.` : "";
      return `open data Afnic${this.fileMonth(f.file_date)}, ${names} noms suivis dont ${registered} enregistrés, ${week} à vérifier dans les 7 jours (date anniversaire).${undated}`;
    },

    fileMonth(v) {
      if (!v) return "";
      // Fichier pris chez l'Afnic : mois au format AAAAMM.
      const m = /^(\d{4})(\d{2})$/.exec(v);
      if (m) return ` (${new Date(+m[1], +m[2] - 1, 1).toLocaleDateString("fr-FR", { month: "long", year: "numeric" })})`;
      return ` (${this.shortDate(v)})`;
    },

    async importFr() {
      try {
        const res = await this.api("POST", "/api/drops/fr/import");
        this.say(res.started ? "Import du .fr lancé. Compter quelques minutes." : "Un import est déjà en cours.");
        this.dropsData.fr.running = true;
        const poll = setInterval(async () => {
          await this.loadDrops();
          if (!this.dropsData || !this.dropsData.fr.running) clearInterval(poll);
        }, 10000);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async saveFr() {
      try {
        const res = await this.api("PUT", "/api/settings/fr", { enabled: this.cfg.fr.enabled, url: this.cfg.fr.url || "", rate: this.cfg.fr.rate || null });
        this.cfg.fr = res;
        if (res.enabled) this.say("Suivi du .fr activé. Premier import dans quelques minutes.");
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async scanDrops() {
      try {
        const res = await this.api("POST", "/api/drops/scan");
        this.say(res.started ? "Lecture des zones lancée. Compter quelques minutes pour .com." : "Une lecture est déjà en cours.");
        this.dropsData.running = true;
        const poll = setInterval(async () => {
          await this.loadDrops();
          if (!this.dropsData || !this.dropsData.running) clearInterval(poll);
        }, 10000);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async watchDrop(d) {
      this.newName = d.name;
      await this.add();
      d.watched = this.domains.some((x) => x.name === d.name);
    },

    async saveCzds() {
      this.savingCzds = true;
      try {
        const res = await this.api("PUT", "/api/settings/czds", { username: this.czdsUser, password: this.czdsPass });
        this.cfg.czds = res;
        this.czdsPass = "";
        this.say(res.notice || `Compte ICANN accepté : ${res.approved.length} zone(s) autorisée(s).`);
      } catch (e) {
        this.say(e.message, "error");
      }
      this.savingCzds = false;
    },

    async deleteCzds() {
      if (!confirm("Retirer le compte ICANN ? Les lectures de zones s'arrêtent.")) return;
      await this.api("DELETE", "/api/settings/czds");
      await this.loadSettings();
    },

    async saveZones() {
      try {
        this.cfg.czds = await this.api("PUT", "/api/settings/czds/zones", { enabled: this.cfg.czds.enabled, hour: this.cfg.czds.hour });
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    toggleZone(t) {
      const list = this.cfg.czds.enabled;
      this.cfg.czds.enabled = list.includes(t) ? list.filter((x) => x !== t) : [...list, t];
      this.saveZones();
    },

    async api(method, url, body) {
      const res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        credentials: "same-origin",
      });
      if (res.status === 401) {
        window.location.href = "/login";
        throw new Error("Session expirée");
      }
      if (!res.ok) {
        let detail = `erreur ${res.status}`;
        try {
          const data = await res.json();
          if (typeof data.detail === "string") detail = data.detail;
        } catch (_) {}
        throw Object.assign(new Error(detail), { status: res.status });
      }
      return res.status === 204 ? null : res.json();
    },

    async load(quiet = false) {
      try {
        const [domains, meta] = await Promise.all([this.api("GET", "/api/domains"), this.api("GET", "/api/meta")]);
        this.domains = domains;
        this.meta = meta;
        this.bulkRunning = meta.bulk_running;
        this.loadError = null;
        this.now = Date.now();
        if (this.openId) this.loadHistory(this.openId);
      } catch (e) {
        if (!quiet || !this.loaded) this.loadError = e.message;
      }
      this.loaded = true;
    },

    say(text, kind = "info") {
      this.notice = { text, kind };
      clearTimeout(this._noticeTimer);
      if (kind !== "error") this._noticeTimer = setTimeout(() => (this.notice = null), 6000);
    },

    async add() {
      const name = this.newName.trim();
      if (!name) return;
      this.adding = true;
      try {
        const d = await this.api("POST", "/api/domains", { name });
        this.domains.push(d);
        this.newName = "";
        this.say(d.check_error ? `${d.name} ajouté. Premier check en échec : ${d.check_error}.` : `${d.name} ajouté : ${this.stateLabel(d)}.`,
          d.check_error ? "error" : "info");
      } catch (e) {
        this.say(e.message, "error");
      }
      this.adding = false;
      this.$nextTick(() => this.$refs.input.focus());
    },

    replace(d) {
      const i = this.domains.findIndex((x) => x.id === d.id);
      if (i >= 0) this.domains[i] = d;
    },

    async check(d) {
      this.busy[d.id] = true;
      try {
        const updated = await this.api("POST", `/api/domains/${d.id}/check`);
        this.replace(updated);
        this.now = Date.now();
        if (updated.check_error) this.say(`${d.name} : ${updated.check_error}.`, "error");
        if (this.openId === d.id) this.loadHistory(d.id);
      } catch (e) {
        this.say(`${d.name} : ${e.message}`, "error");
      }
      this.busy[d.id] = false;
    },

    async checkAll() {
      try {
        const res = await this.api("POST", "/api/check-all");
        this.bulkRunning = true;
        this.say(res.started ? "Vérification de tous les domaines lancée." : "Une vérification globale est déjà en cours.");
        const poll = setInterval(async () => {
          await this.load(true);
          if (!this.bulkRunning) {
            clearInterval(poll);
            this.say("Vérification globale terminée.");
          }
        }, 5000);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async remove(d) {
      if (!confirm(`Supprimer ${d.name} de la surveillance ? L'historique sera effacé.`)) return;
      this.busy[d.id] = true;
      try {
        await this.api("DELETE", `/api/domains/${d.id}`);
        this.domains = this.domains.filter((x) => x.id !== d.id);
        if (this.openId === d.id) this.openId = null;
        this.say(`${d.name} supprimé.`);
      } catch (e) {
        this.say(`${d.name} : ${e.message}`, "error");
      }
      this.busy[d.id] = false;
    },

    async toggleSettings() {
      this.settingsOpen = true;
      if (this.settingsOpen) await this.loadSettings();
    },

    async loadSettings() {
      try {
        this.cfg = await this.api("GET", "/api/settings");
        this.czdsUser = this.cfg.czds.username;
        this.orderUrl = this.cfg.order_url;
        this.loadAiStatus();
      } catch (e) {
        this.say(`Paramètres indisponibles : ${e.message}`, "error");
      }
    },

    async saveToken() {
      this.savingToken = true;
      try {
        const res = await this.api("PUT", "/api/settings/telegram", { token: this.tokenInput });
        this.tokenInput = "";
        this.editingToken = false;
        this.say(`Bot @${res.bot_name} enregistré. Associer maintenant ton compte.`);
        await this.loadSettings();
      } catch (e) {
        this.say(e.message, "error");
      }
      this.savingToken = false;
    },

    async startPairing() {
      try {
        this.pairing = await this.api("POST", "/api/settings/telegram/pair");
      } catch (e) {
        this.say(e.message, "error");
        return;
      }
      clearInterval(this._pairTimer);
      // Le serveur efface le code dès que le bon message arrive, ou au bout de 10 minutes.
      this._pairTimer = setInterval(async () => {
        await this.loadSettings();
        if (this.cfg && !this.cfg.telegram.pairing) {
          clearInterval(this._pairTimer);
          this.pairing = null;
          if (this.cfg.telegram.chat_set) this.say("Compte Telegram associé.");
        }
      }, 3000);
    },

    async removeTelegram() {
      if (!confirm("Retirer le bot Telegram ? Les alertes ne seront plus envoyées.")) return;
      await this.api("DELETE", "/api/settings/telegram");
      this.pairing = null;
      await this.loadSettings();
      this.say("Bot retiré.");
    },

    async saveDigest() {
      try {
        this.cfg = await this.api("PUT", "/api/settings/digest", {
          enabled: this.cfg.digest.enabled, day: this.cfg.digest.day, hour: this.cfg.digest.hour,
        });
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async sendDigest() {
      try {
        await this.api("POST", "/api/digest/send");
        this.say("Résumé envoyé sur Telegram.");
      } catch (e) {
        this.say(`Résumé non envoyé : ${e.message}`, "error");
      }
    },

    get versionState() {
      if (!this.meta || !this.meta.latest_version) return "unknown";
      return this.meta.update_available ? "outdated" : "ok";
    },

    get versionTitle() {
      return { ok: "À jour", outdated: "Mise à jour disponible", unknown: "Dernière version publiée inconnue" }[this.versionState];
    },

    get versionStatus() {
      if (!this.meta) return "";
      return { ok: "À jour.", outdated: "Mise à jour disponible.", unknown: "Dernière version publiée inconnue." }[this.versionState];
    },

    stamp(v) {
      return v ? `${this.relative(v)} (${stampFmt.format(new Date(v))})` : "jamais";
    },

    async checkVersion() {
      this.checkingVersion = true;
      try {
        this.meta = await this.api("POST", "/api/version/check");
        if (!this.meta.latest_version) this.say("Docker Hub injoignable, version publiée inconnue.", "error");
      } catch (e) {
        this.say(e.message, "error");
      }
      this.checkingVersion = false;
    },

    async saveAppearance() {
      try {
        const { app_name, theme, logo_height } = this.cfg.appearance;
        this.cfg = await this.api("PUT", "/api/settings/appearance", { app_name, theme, logo_height });
        this.applyAppearance();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    // Aperçu en direct : la variable CSS change l'en-tête et l'aperçu sans attendre le serveur.
    previewLogo() {
      document.documentElement.style.setProperty("--logo-h", `${this.cfg.appearance.logo_height}px`);
    },

    get logoSrc() {
      const img = document.querySelector(".top .logo");
      return img ? img.getAttribute("src") : "/logo";
    },

    applyAppearance() {
      this.previewLogo();
      const { app_name, theme } = this.cfg.appearance;
      const root = document.documentElement;
      if (theme === "system") delete root.dataset.theme;
      else root.dataset.theme = theme;
      document.querySelector('meta[name="color-scheme"]').content = theme === "system" ? "light dark" : theme;
      document.querySelectorAll(".appname").forEach((el) => { el.textContent = app_name; });
      document.title = app_name;
    },

    async uploadLogo(event) {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      const body = new FormData();
      body.append("file", file);
      const res = await fetch("/api/settings/logo", { method: "POST", body, credentials: "same-origin" });
      if (!res.ok) {
        let detail = `erreur ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        this.say(`Logo refusé : ${detail}`, "error");
        return;
      }
      // Le logo est rendu côté serveur dans l'en-tête : un rechargement l'affiche.
      window.location.reload();
    },

    async removeLogo() {
      await this.api("DELETE", "/api/settings/logo");
      window.location.reload();
    },

    async regenerateCalendar() {
      if (this.cfg.calendar_url && !confirm("Changer l'adresse ? L'ancienne cessera de fonctionner.")) return;
      const res = await this.api("POST", "/api/settings/calendar");
      this.cfg.calendar_url = res.calendar_url;
    },

    async copy(text) {
      try {
        await navigator.clipboard.writeText(text);
        this.say("Adresse copiée.");
      } catch (_) {
        this.say("Copie impossible : sélectionner l'adresse et la copier à la main.", "error");
      }
    },

    async addTag(d, input) {
      const tag = input.value.trim();
      if (!tag) return;
      input.value = "";
      await this.saveTags(d, [...d.tags, tag]);
    },

    async removeTag(d, tag) {
      await this.saveTags(d, d.tags.filter((t) => t !== tag));
      if (this.tagFilter === tag && !this.domains.some((x) => x.tags.includes(tag))) this.tagFilter = null;
    },

    async saveTags(d, tags) {
      try {
        this.replace(await this.api("PUT", `/api/domains/${d.id}/tags`, { tags }));
      } catch (e) {
        this.say(`${d.name} : ${e.message}`, "error");
      }
    },

    async refreshSignals(d) {
      this.busySignals[d.id] = true;
      try {
        this.replace(await this.api("POST", `/api/domains/${d.id}/signals`));
      } catch (e) {
        this.say(`${d.name} : ${e.message}`, "error");
      }
      this.busySignals[d.id] = false;
    },

    async testAlert() {
      this.sendingTest = true;
      try {
        await this.api("POST", "/api/alerts/test");
        this.say("Alerte test envoyée sur Telegram.");
      } catch (e) {
        this.say(`Alerte test non envoyée : ${e.message}`, "error");
      }
      this.sendingTest = false;
    },

    toggle(d) {
      this.openId = this.openId === d.id ? null : d.id;
      if (this.openId) this.loadHistory(d.id);
    },

    async loadHistory(id) {
      try {
        this.history[id] = await this.api("GET", `/api/domains/${id}/history`);
      } catch (_) {
        this.history[id] = [];
      }
    },

    displayState(d) {
      if (d.check_error) return "error";
      return d.state || "unknown";
    },

    stateLabel(d) {
      if (d.check_error) return LABELS.error;
      return LABELS[d.state] || "Non vérifié";
    },

    stateTitle(d) {
      if (d.check_error) return `${d.check_error}. Dernier état connu conservé.`;
      if (d.state_since) return `Dans cet état depuis le ${this.exact(d.state_since)}`;
      return "";
    },

    get counters() {
      const counts = {};
      for (const d of this.domains) {
        const key = this.displayState(d);
        counts[key] = (counts[key] || 0) + 1;
      }
      return [...URGENCY, "error"].filter((k) => counts[k]).map((k) => ({ key: k, label: LABELS[k], count: counts[k] }));
    },

    get legend() {
      const iv = (this.meta && this.meta.intervals) || {};
      const days = (this.meta && this.meta.expiring_days) || 30;
      const f = (k) => (iv[k] ? frequency(iv[k]) : "");
      return [
        { key: "available", definition: "Le registre ne connaît pas ce nom : probablement enregistrable maintenant.", frequency: f("available"), action: "Enregistrer le nom chez un registrar." },
        { key: "pending_delete", definition: "Plus aucun renouvellement ni restauration possible. Libération dans quelques jours.", frequency: f("pending_delete"), action: "Préparer l'achat ou poser un backorder." },
        { key: "redemption", definition: "Nom supprimé par le registrar. Le titulaire peut encore le restaurer, avec frais.", frequency: f("redemption"), action: "Repérer un service de backorder. La plupart des noms de valeur sont restaurés ici." },
        { key: "expired", definition: "Date d'expiration dépassée, période de grâce. Le titulaire peut encore renouveler.", frequency: f("expired"), action: "Attendre. Beaucoup de noms sont renouvelés pendant cette période." },
        { key: "expiring", definition: `Enregistré, expiration dans moins de ${days} jours.`, frequency: f("expiring"), action: "Surveiller. Les renouvellements de dernière minute sont fréquents." },
        { key: "active", definition: `Enregistré, expiration dans plus de ${days} jours.`, frequency: f("active"), action: "Rien à faire." },
        { key: "error", definition: "Le dernier check a échoué (réseau, limite de requêtes, serveur). Statut inconnu, dernier état connu conservé.", frequency: "selon le dernier état", action: "Rien : le check est retenté. Vérifier à la main si l'erreur persiste." },
      ].map((x) => ({ ...x, label: LABELS[x.key] }));
    },

    daysLeft(d) {
      if (!d.effective_expires_at || d.state === "available") return null;
      return Math.ceil((new Date(d.effective_expires_at).getTime() - this.now) / DAY);
    },

    sortValue(d, key) {
      switch (key) {
        case "name": return d.unicode_name;
        case "days": { const v = this.daysLeft(d); return v === null ? Infinity : v; }
        case "release": return d.release_at ? new Date(d.release_at).getTime() : Infinity;
        case "next": return d.next_check_at ? new Date(d.next_check_at).getTime() : Infinity;
        default: { const i = URGENCY.indexOf(d.state); return i < 0 ? URGENCY.length : i; }
      }
    },

    // Filtres gardés dans le navigateur d'une visite à l'autre.
    restoreFilters() {
      try {
        const saved = JSON.parse(localStorage.getItem("dw.filters") || "{}");
        this.query = saved.query || "";
        this.filter = saved.filter || null;
        this.tagFilter = saved.tagFilter || null;
        this.abandonedOnly = !!saved.abandonedOnly;
        if (saved.sortKey) { this.sortKey = saved.sortKey; this.sortDir = saved.sortDir || 1; }
      } catch (_) {}
    },

    saveFilters() {
      try {
        localStorage.setItem("dw.filters", JSON.stringify({
          query: this.query, filter: this.filter, tagFilter: this.tagFilter,
          abandonedOnly: this.abandonedOnly, sortKey: this.sortKey, sortDir: this.sortDir,
        }));
      } catch (_) {}
    },

    get activeFilters() {
      const list = [];
      if (this.query.trim()) list.push({ key: "query", label: `Nom : ${this.query.trim()}` });
      if (this.filter) list.push({ key: "filter", label: `État : ${LABELS[this.filter] || this.filter}` });
      if (this.tagFilter) list.push({ key: "tagFilter", label: `Tag : ${this.tagFilter}` });
      if (this.abandonedOnly) list.push({ key: "abandonedOnly", label: "Signes d'abandon" });
      return list;
    },

    clearFilter(key) {
      if (key === "query") this.query = "";
      else if (key === "abandonedOnly") this.abandonedOnly = false;
      else this[key] = null;
    },

    clearFilters() {
      this.query = "";
      this.filter = null;
      this.tagFilter = null;
      this.abandonedOnly = false;
    },

    get allTags() {
      return [...new Set(this.domains.flatMap((d) => d.tags))].sort();
    },

    get rows() {
      const q = this.query.trim().toLowerCase();
      const list = this.domains.filter((d) =>
        (!this.filter || this.displayState(d) === this.filter) &&
        (!this.tagFilter || d.tags.includes(this.tagFilter)) &&
        (!this.abandonedOnly || d.abandoned) &&
        (!q || d.name.includes(q) || d.unicode_name.toLowerCase().includes(q)));
      const key = this.sortKey;
      const dir = this.sortDir;
      return list.sort((a, b) => {
        const va = this.sortValue(a, key);
        const vb = this.sortValue(b, key);
        if (va < vb) return -dir;
        if (va > vb) return dir;
        // À urgence égale, le plus proche de l'échéance d'abord.
        const da = this.sortValue(a, "days");
        const db = this.sortValue(b, "days");
        if (da !== db) return da < db ? -1 : 1;
        return a.unicode_name.localeCompare(b.unicode_name);
      });
    },

    sortBy(key) {
      if (this.sortKey === key) this.sortDir = -this.sortDir;
      else { this.sortKey = key; this.sortDir = 1; }
    },

    arrow(key) {
      return this.sortKey === key ? (this.sortDir === 1 ? " ↑" : " ↓") : "";
    },

    releaseLong(d) {
      let text = "~" + this.longDate(d.release_at);
      if (d.release_has_time) text += " vers " + timeFmt.format(new Date(d.release_at));
      if (d.release_is_upper_bound) text += " au plus tard";
      return text;
    },

    releaseTitle(d) {
      const tld = d.name.split(".").pop();
      let t = `Estimation. Calculée depuis le statut du domaine et les délais publiés par le registre du .${tld}.`;
      if (d.release_is_upper_bound) t += " Date au plus tard : le registrar peut supprimer le nom plus tôt.";
      if (d.release_has_time) t += ` Heure : début de la fenêtre quotidienne de suppression du registre, vers ${timeFmt.format(new Date(d.release_at))} heure locale (source presse, non officielle).`;
      return `${t} ${this.exact(d.release_at)}`;
    },

    noReleaseReason(d) {
      if (!d.state || d.state === "available" || d.state === "active") return "Pas d'estimation dans cet état.";
      const tld = d.name.split(".").pop();
      return `Pas d'estimation : délais du .${tld} non sourcés.`;
    },

    eppExplain(s) {
      return EPP[s.replace(/[\s_]/g, "").toLowerCase()] || "Statut sans explication connue.";
    },

    describe(h) {
      const field = FIELDS[h.field] || h.field;
      const fmt = (v) => {
        if (v === null || v === undefined || v === "" || v === "[]") return "aucun";
        if (h.field === "state") return LABELS[v] || v;
        if (h.field === "expires_at") return dateFmt.format(new Date(v));
        if (h.field === "nameservers") { try { return JSON.parse(v).join(", "); } catch (_) { return v; } }
        return v;
      };
      return `${field} : ${fmt(h.old_value)} → ${fmt(h.new_value)}`;
    },

    exact(v) { return v ? exactFmt.format(new Date(v)) : ""; },
    shortDate(v) { return v ? dateFmt.format(new Date(v)) : ""; },
    longDate(v) { return v ? longFmt.format(new Date(v)) : "Inconnue"; },

    relative(v) {
      if (!v) return "";
      const diff = new Date(v).getTime() - this.now;
      const abs = Math.abs(diff);
      const past = diff < 0;
      const min = Math.round(abs / 60000);
      // Moins d'une minute : quelques secondes d'écart d'horloge entre le NAS et le téléphone.
      if (min < 1) return "à l'instant";
      let text;
      if (min < 60) text = `${min} min`;
      else if (min < 60 * 24) text = `${Math.round(min / 60)} h`;
      else if (min < 60 * 24 * 60) text = `${Math.round(min / 1440)} j`;
      else return this.shortDate(v);
      return past ? `il y a ${text}` : `dans ${text}`;
    },
  }));
});
