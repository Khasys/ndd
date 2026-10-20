// Filtre « avis IA » : verdicts et bornes min/max de chaque note et de chaque prix (paramètres d'URL ai_*).
const AI_FILTER_DEFAULTS = Object.fromEntries([["ai_v", ""], ...["exploit", "resale", "quick_low", "quick_high", "final_low", "final_high"]
  .flatMap((f) => [[`ai_${f}_min`, ""], [`ai_${f}_max`, ""]])]);
const AI_VERDICTS = { take: "À prendre", watch: "À surveiller", skip: "À laisser" };
// Sous-notes de la grille v5 (sur 10), dans l'ordre de la consigne.
const AI_GRID = [["oral", "prononciation"], ["radio", "test radio"], ["memo", "mémorisation"], ["sens", "sens"],
  ["marque", "marque"], ["marche", "marché"], ["ext", "extension"], ["acheteurs", "acheteurs"], ["risque", "risque"]];
// Catégories de forme ; les autres viennent des listes de noms.
const SHAPE_KINDS = ["letters", "digits", "mixed", "hyphen"];
// Extensions sans zone, lues dans le DNS du registre : absent du DNS ne veut pas dire libre.
const DNS_TLDS = ["co", "ai", "io"];
// Libellés des valeurs brutes (catégories, nature) : la fiche s'ouvre aussi depuis Domaines, avant que la
// Veille ou l'Explorer aient chargé les leurs. Mêmes mots que drops.KINDS et wordlists.LABELS / NATURES.
const KIND_LABELS = {
  letters: "Lettres", digits: "Chiffres", mixed: "Lettres et chiffres", hyphen: "Avec tiret", word: "Mots",
  form: "Formes fléchies", surname: "Noms de famille", firstname: "Prénoms", english: "Mots anglais",
  sector: "Secteurs porteurs", trend: "Tendances", combo: "Noms composés", niche: "Sites de niche", expr: "Expressions",
  media: "Noms de médias", startup: "Tech et startups", job: "Métiers", sport: "Sport", animal: "Animaux", food: "Cuisine",
  premium: "Termes stars", keyword: "Mots-clés à forte valeur", local: "Métier + ville", brand: "Noms de marque", mine: "Mes ajouts",
};
const NATURE_LABELS = { noun: "nom commun", verb: "verbe", adj: "adjectif", adv: "adverbe", other: "mot outil" };
// Rang dans une liste, en clair : « 12e prénom le plus donné » (mêmes mots que les raisons du score, value.py).
const FREQ_PHRASES = {
  word: "mot le plus courant", form: "forme la plus courante", surname: "nom de famille le plus porté",
  firstname: "prénom le plus donné", english: "mot anglais le plus courant", sector: "terme star", trend: "tendance",
  combo: "nom composé", niche: "site de niche", expr: "expression courante", media: "nom de média",
  startup: "mot tech et startups", job: "métier le plus demandé", sport: "mot du sport", animal: "animal", food: "mot de cuisine",
  premium: "terme star", keyword: "mot-clé à forte valeur", local: "métier + ville", brand: "nom de marque", mine: "de mes ajouts",
};
const VERDICT_CLASSES = { "À prendre": "take", "À surveiller": "watch", "À laisser": "skip" };
// min : score minimum des filtres avancés ; 0 = tout voir. period : libérés depuis / libération dans (clé de
// PERIODS), "" = tout ce que la Veille garde.
// nopremium : « Masquer les premiums » ; gprice : prix d'achat Gandi au plus (noms vérifiés chez Gandi seulement).
const DROP_FILTER_DEFAULTS = { q: "", nat: "", lang: "", len: "", rank: "", min: 0, junk: false, ai: [], quick: "", nopremium: false, gprice: "", period: "" };
// Réponses de Gandi qui disent qu'on ne peut pas acheter le nom (gandi.BLOCKED).
const GANDI_BLOCKED = ["unavailable", "reserved", "restricted"];
// Ce que ça veut dire, le registre le disant libre (sinon la revérification l'aurait fait passer « pris ») :
// prunes.xyz, libre au registre, vendu 1 271 € HT par un tiers chez OVH.
const BLOCKED_WHY = "libre au registre, mais pas au prix normal : premium ou vente par un tiers";
// Tris de la Veille ; « release » ne vaut que pour Bientôt libres.
const DROP_SORTS = ["best", "recent", "release", "freq"];
// Périodes d'Aujourd'hui et de la Veille (paramètre period de /api/today et /api/drops?focus=…) : libérés depuis,
// libération dans, nouveaux « À prendre » depuis. long : la période en toutes lettres dans les libellés (espace
// insécable : « 7 jours » ne se coupe pas dans les tuiles du téléphone).
const PERIODS = [{ k: "24h", label: "24 h", long: "24\u00a0h", hours: 24 }, { k: "7d", label: "7 j", long: "7\u00a0jours", hours: 168 },
  { k: "30d", label: "30 j", long: "30\u00a0jours", hours: 720 }];
// Chiffres d'Aujourd'hui : la Veille s'ouvre sur exactement leurs noms (/api/drops?focus=…&period=…), filtres mis de côté.
const DROP_FOCUS = { freed: "Libérés depuis", coming: "Libération sous", new_take: "Nouveaux « À prendre » depuis" };
// Filtres vierges de l'Explorer ; l'extension choisie reste.
// nopremium : « Masquer les premiums » (vus chez Gandi ou probables) ; gprice : prix d'achat Gandi au plus.
const EX_BLANK = { cat: "", nat: "", lang: "", len: "", rank: "", taken: "", min: "", junk: "", seo: "", nopremium: "", gprice: "", status: "bookable", sort: "best", level: "good", ...AI_FILTER_DEFAULTS };
// Tris de la liste ; « best » suit l'usage (revendre / exploiter) ; « recent » : prénoms de la dernière année Insee.
const EX_SORTS = ["best", "release", "length", "freq", "recent", "score"];
// Explorer et Veille : trois réglages simples, les mêmes partout.
const EX_STATES = [{ k: "bookable", label: "Libres" }, { k: "coming", label: "Bientôt libres" }, { k: "all", label: "Tous, même pris" }];
const EX_TYPES = [
  { k: "", label: "Toutes" }, { k: "word", label: "Mots courants" }, { k: "english", label: "Anglais" },
  { k: "firstname", label: "Prénoms" }, { k: "surname", label: "Noms de famille" }, { k: "startup", label: "Tech et startups" },
  { k: "media", label: "Noms de médias" }, { k: "job", label: "Métiers" },
  { k: "sport", label: "Sport" }, { k: "animal", label: "Animaux" }, { k: "food", label: "Cuisine" }, { k: "trend", label: "Tendances" },
  { k: "niche", label: "Sites de niche" }, { k: "expr", label: "Expressions" }, { k: "premium", label: "Termes stars" },
  { k: "keyword", label: "Mots-clés à forte valeur" }, { k: "local", label: "Métier + ville" }, { k: "brand", label: "Noms de marque" },
  { k: "mine", label: "Mes ajouts" }, { k: "short", label: "Courts (1 à 4)" },
];
// Listes sans puce, lues comme une autre (mêmes valeurs que wordlists.ALIASES) : vues enregistrées, mémoire du navigateur.
const CAT_ALIASES = { sector: "premium" };
// Listes du Top (Classements), dans l'ordre des puces de type ; firstname_recent : « Prénoms <année> » (Insee), si lus.
const TOP_LISTS = ["premium", "job", "sport", "animal", "food", "trend", "expr", "niche", "keyword", "local", "brand", "mine",
  "word", "english", "firstname", "firstname_recent", "surname", "combo", "form"];
// Libellé d'un nom ajoutable à Mes ajouts (wordlists.normalize côté serveur, qui ôte aussi les accents).
const LABEL_OK = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
// Erreurs réseau en français simple : le détail technique reste dans la console du navigateur, qui
// journalise la requête de toute façon ; l'appli n'y ajoute rien.
const PLAIN_ERRORS = [
  [/ProxyError|ConnectError|ConnectTimeout|Bootstrap IANA indisponible|injoignable|Failed to fetch|NetworkError|Load failed|ECONN|getaddrinfo|Name or service not known|RemoteProtocolError|ReadError|TransportError/i, "Réseau injoignable"],
  [/timeout|timed out|délai dépassé|pas de réponse|aucune réponse/i, "Pas de réponse du registre"],
];
function plainError(message) {
  const text = String(message || "");
  for (const [re, plain] of PLAIN_ERRORS) if (re.test(text)) return plain;
  return text;
}
// Le détail brut (serveur, type d'erreur) quand plainError l'a résumé ; vide sinon. Affiché au survol
// et en ligne « Détail : … » (message, fiche), pour savoir quel serveur ne répond pas.
function errorDetail(message) {
  const text = String(message || "");
  return plainError(text) !== text ? text : "";
}
// Une action est rejouable quand l'échec vient du réseau ou du serveur, pas d'une réponse attendue (409, 422).
function retryable(e) {
  return !e.status || e.status >= 500 || e.status === 429 || plainError(e.message) !== String(e.message || "");
}
// « 3 vérifiés, 1 échec » : bilan d'une action groupée.
function tally(ok, fail, one, many) {
  const parts = [`${ok} ${ok > 1 ? many : one}`];
  if (fail) parts.push(`${fail} échec${fail > 1 ? "s" : ""}`);
  return parts.join(", ");
}
const LENSES = [{ k: "resale", label: "Pour revendre" }, { k: "exploit", label: "Pour exploiter" }];
const LABELS = {
  available: "Disponible",
  // Veille : libre d'après le seul WHOIS (drops.UNCONFIRMED), pas « libéré ».
  unconfirmed: "Libre selon le WHOIS, à confirmer",
  pending_delete: "Suppression imminente",
  redemption: "En rédemption",
  expired: "Expiré",
  renewed: "Renouvelé",
  expiring: "Expire bientôt",
  active: "Actif",
  error: "Erreur de check",
};

const URGENCY = ["available", "pending_delete", "redemption", "expired", "renewed", "expiring", "active"];

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
  // x-tpl="['id', { c: … }]" ou x-tpl="'id'" : clone le <template id="…"> (une seule racine) dans
  // l'élément, avec les données passées en portée ; les méthodes viennent du composant parent.
  // Un seul gabarit HTML pour la carte de nom (Veille, Explorer, recherche) et les réglages simples.
  Alpine.directive("tpl", (el, { expression }, { evaluateLater, effect }) => {
    const get = evaluateLater(expression);
    const scope = Alpine.reactive({});
    let root = null;
    effect(() => get((spec) => {
      const [id, data] = Array.isArray(spec) ? spec : [spec, {}];
      Object.assign(scope, data);
      if (root) return;
      root = document.getElementById(id).content.firstElementChild.cloneNode(true);
      Alpine.addScopeToNode(root, scope, el);
      Alpine.mutateDom(() => { el.appendChild(root); Alpine.initTree(root); });
    }));
  });

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
    history: {},
    busy: {},
    // Action de plus de 2 s : { left: secondes restantes estimées ou null, frac: part écoulée } (voir track).
    loader: null,
    bulkRunning: false,
    sendingTest: false,
    // Écran d'accueil : les meilleurs noms du jour (GET /api/today) ; l'appli s'ouvre toujours dessus.
    todayOpen: true,
    today: null,
    // Période d'Aujourd'hui (clé de PERIODS), mémorisée par le navigateur ; 24 h par défaut.
    todayPeriod: (() => { try { const p = localStorage.getItem("today.period"); return PERIODS.some((x) => x.k === p) ? p : "24h"; } catch (_) { return "24h"; } })(),
    // « Masquer les premiums » d'Aujourd'hui (décoché par défaut), mémorisé par le navigateur.
    todayNoPremium: (() => { try { return localStorage.getItem("today.nopremium") === "1"; } catch (_) { return false; } })(),
    PERIODS,
    // Démarrer en 3 étapes (sources, Telegram, avis IA) : /api/meta et /api/today, même calcul serveur.
    setup: [],
    legendOpen: false,
    settingsOpen: false,
    settingsAdvanced: false,
    dropsOpen: false,
    dropsData: null,
    exploreOpen: false,
    portfolioOpen: false,
    pf: null,
    pfNew: { name: "", buy_price: "", bought_on: "", renew_price: "" },
    // Noms ajoutés au portefeuille dont le registre est interrogé en arrière-plan (jusqu'au rechargement suivant).
    pfChecking: [],
    decisions: null,
    aiOutcomes: null,
    savingAlerts: false,
    gemsAll: false,
    // Domaines : sélection multiple, actions groupées.
    domSelected: [],
    domBulkRunning: false,
    domBulkDone: 0,
    TOP_LISTS,
    exData: null,
    exError: "",
    exItems: [],
    exLimit: 50,
    exNoMore: false,
    exTopOpen: false,
    top: { cat: "premium", n: 50 },
    topData: null,
    exLoading: false,
    exCard: null,
    exMap: null,
    exBusy: {},
    mineBusy: false,
    exSeo: { running: false, done: 0, total: 0, key_set: true },
    seoKey: "",
    ex: { tld: "", q: "", status: "bookable", cat: "", len: "", sort: "best", nat: "", rank: "", taken: "", min: "", junk: "", seo: "", lang: "", nopremium: "", gprice: "", level: "good", ...AI_FILTER_DEFAULTS },
    lens: (() => { try { return localStorage.getItem("lens") || "resale"; } catch (_) { return "resale"; } })(),
    EX_STATES, EX_TYPES, LENSES,
    exFiltersOpen: false,
    exMapOpen: false,
    fiche: null,
    // Fiche : menu « Plus » ouvert, dates clés toutes affichées, détail IA déroulé (gardé d'une fiche à l'autre).
    ficheMenu: false,
    ficheTlAll: false,
    ficheAiOpen: false,
    // Paramètres, Gandi : jeton saisi (jamais relu), test en cours, résumé du dernier test.
    gandiKey: "",
    gandiTesting: false,
    gandiResults: [],
    gandiStarting: false,
    aiReq: { pending: [], done: [] },
    aiPanel: false,
    dropTab: "free",
    // Chiffre d'Aujourd'hui ouvert dans la Veille (clé de DROP_FOCUS), "" sinon.
    dropFocus: "",
    // Période du chiffre ouvert : celle d'Aujourd'hui au moment du clic ; premiums masqués ou non, comme Aujourd'hui.
    dropFocusPeriod: "24h",
    dropFocusNoPremium: false,
    DROP_FOCUS,
    // Décisions du Portefeuille : seulement les regrets (noms ignorés repris), depuis Aujourd'hui.
    decisionsRegrets: false,
    dropNewOnly: false,
    dropLevel: "good",
    dropSortMode: "best",
    // Cartes affichées d'abord : une quarantaine suffit à remplir l'écran, chaque carte coûte au premier affichage.
    dropShow: 40,
    dropSelected: [],
    dropBulkRunning: false,
    dropBulkDone: 0,
    dropsSeenBefore: "",
    // Jetons d'accès (Paramètres) : liste, type et libellé du prochain, jeton tout juste créé (montré une fois).
    tokenList: null,
    tokenKind: "mcp",
    tokenLabel: "",
    tokenNew: null,
    tokenEdit: null,
    tokenEditLabel: "",
    aiStatus: null,
    df: { ...DROP_FILTER_DEFAULTS },
    dfOpen: false,
    AI_VERDICTS,
    tldFilter: "",
    aiFilter: [],
    exSelected: [],
    exBulkRunning: false,
    exBulkDone: 0,
    exViews: [],
    // « -4 » est aussi la puce « Courts (1 à 4) » de l'Explorer : même libellé des deux côtés.
    exLengths: [{ v: "", label: "Toutes" }, { v: "-4", label: "Courts (1 à 4)" }, { v: "-3", label: "1 à 3" }, { v: "1", label: "1" }, { v: "2", label: "2" }, { v: "3", label: "3" }, { v: "4", label: "4" }, { v: "5", label: "5 et plus" }],
    exRanks: [{ v: "", label: "Toutes" }, { v: "100", label: "Top 100" }, { v: "500", label: "Top 500" }, { v: "1000", label: "Top 1 000" }, { v: "5000", label: "Top 5 000" }],
    orderUrl: "",
    // Extensions cochées dans la Veille, plusieurs à la fois ; vide : toutes.
    dropTld: [],
    dropKinds: [],
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
    isMobile: window.matchMedia("(max-width: 899px)").matches,
    // Nom tapé dans l'Explorer, absent des listes : une ligne par extension, à vérifier.
    exProbe: [],

    init() {
      // Une requête lancée juste après un geste (ou à l'ouverture) est une action : le cercle de chargement la suit ;
      // les rafraîchissements de fond n'en montrent pas.
      this._lastInput = Date.now();
      for (const ev of ["pointerdown", "keydown", "input", "change"]) {
        window.addEventListener(ev, () => (this._lastInput = Date.now()), { capture: true, passive: true });
      }
      // Téléphone ou ordinateur : suivi en direct, les réglages simples changent de place selon la largeur.
      const narrow = window.matchMedia("(max-width: 899px)");
      narrow.addEventListener("change", (e) => (this.isMobile = e.matches));
      this.restoreFilters();
      for (const key of ["query", "filter", "tagFilter", "abandonedOnly", "sortKey", "sortDir", "tldFilter", "aiFilter"]) {
        this.$watch(key, () => this.saveFilters());
      }
      this.dropRestore();
      // La liste change : la sélection en masse ne doit pas survivre à des noms qui ne s'affichent plus.
      for (const key of ["df", "dropTld", "dropKinds"]) {
        this.$watch(key, () => { this.dropSave(); this.dropSelected = []; });
      }
      for (const key of ["dropLevel", "dropNewOnly"]) this.$watch(key, () => (this.dropSelected = []));
      for (const key of ["dropLevel", "dropSortMode"]) this.$watch(key, () => this.dropSave());
      this.load();
      this.loadToday();
      this.loadAiRequests();
      // Lien direct vers une fiche (message Telegram d'une analyse prête) : ouverte, puis adresse nettoyée.
      const linked = new URLSearchParams(window.location.search);
      if (linked.get("fiche")) {
        this.openFiche(linked.get("fiche"), linked.get("ai") === "1");
        history.replaceState(null, "", "/");
      }
      // Sur téléphone, le focus automatique ouvrirait le clavier à chaque lancement.
      if (!this.mobile && !this.paged) this.$nextTick(() => this.$refs.input.focus());
      setInterval(() => (this.now = Date.now()), 30000);
      setInterval(() => { this.load(true); this.loadAiRequests(); }, 60000);
      // Appli installée : retour au premier plan après une pause, données à rafraîchir.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") { this.now = Date.now(); this.load(true); }
      });
      this.$watch("todayOpen", () => this.lockScroll());
      this.$watch("legendOpen", () => this.lockScroll());
      this.$watch("settingsOpen", () => this.lockScroll());
      this.$watch("dropsOpen", () => this.lockScroll());
      this.$watch("exploreOpen", () => this.lockScroll());
      this.$watch("portfolioOpen", () => this.lockScroll());
      // Veille ouverte : le travail .fr est suivi en direct.
      setInterval(() => { if (this.dropsOpen && document.visibilityState === "visible") this.loadDrops(true); }, 15000);
      this.$watch("fiche", (f) => {
        this.lockScroll();
        // Fiche ouverte : le clavier va dessus, pas sur la liste derrière.
        if (f) this.$nextTick(() => document.querySelector(".fiche-close")?.focus({ preventScroll: true }));
      });
      window.addEventListener("popstate", () => this.back());
      // Ordinateur : un clic hors de la fiche la ferme (appui noté au début du geste, décision au clic).
      document.addEventListener("pointerdown", (e) => this.ficheDown(e), true);
      document.addEventListener("click", (e) => this.ficheOutside(e));
      if ("serviceWorker" in navigator && window.isSecureContext) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
    },

    get mobile() { return this.isMobile; },

    // Zones qui ne ferment pas la fiche : elle-même, le message du bas, une boîte de dialogue ou un menu.
    ficheKeeps(el) {
      return !(el instanceof Element) || !!el.closest("aside.fiche, .notice, [role=dialog], [role=alertdialog], [role=menu]");
    },

    ficheDown(e) {
      // Sur téléphone la fiche est plein écran : rien n'est « dehors ».
      this._ficheDown = this.fiche && !this.mobile && e.button === 0 && !this.ficheKeeps(e.target)
        ? { ticket: this._ficheTicket, menu: this.ficheMenu } : null;
    },

    ficheOutside(e) {
      const down = this._ficheDown;
      this._ficheDown = null;
      if (!down || !this.fiche || this.mobile || this.ficheKeeps(e.target)) return;
      // Le clic a demandé une autre fiche (un nom d'une liste) : elle s'ouvre, rien à fermer.
      if (down.ticket !== this._ficheTicket) return;
      // Sélection de texte en cours : le geste servait à sélectionner, pas à fermer.
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim()) return;
      // Menu « Plus » ouvert : le premier clic dehors ne ferme que lui, comme Échap.
      if (down.menu) return;
      this.fiche = null;
    },

    lockScroll() {
      // La fiche est par-dessus tout : la page derrière ne défile plus, sur ordinateur aussi.
      document.body.classList.toggle("sheet-open", (this.mobile && this.paged) || this.fiche !== null);
    },

    // Fiche d'un nom surveillé : sa ligne de Domaines (dates, registrar, DNS, tags, signaux), sinon null.
    get ficheDomain() {
      return this.fiche && this.fiche.domain_id ? this.domains.find((d) => d.id === this.fiche.domain_id) || null : null;
    },

    plainError(m) { return plainError(m); },
    errorDetail(m) { return errorDetail(m); },

    escape() {
      // Menu « Plus » de la fiche ouvert : Échap le ferme, la fiche reste.
      if (this.fiche && this.ficheMenu) this.ficheMenuClose(true);
      else if (this.fiche) this.fiche = null;
      else if (this.exploreOpen && this.exFiltersOpen) this.exFiltersOpen = false;
      else if (this.dropsOpen && this.dfOpen) this.dfOpen = false;
      else if (this.exploreOpen && this.ex.q) { this.ex.q = ""; this.exTyped(); }
      else if (!this.todayOpen) this.openSheet("today", false);
    },

    // Domaines est la liste, affichée quand aucun autre écran ne l'est.
    get paged() { return this.todayOpen || this.legendOpen || this.settingsOpen || this.dropsOpen || this.exploreOpen || this.portfolioOpen; },

    showList() {
      this.todayOpen = false;
      this.legendOpen = false;
      this.settingsOpen = false;
      this.dropsOpen = false;
      this.exploreOpen = false;
      this.portfolioOpen = false;
      window.scrollTo({ top: 0, behavior: "smooth" });
      document.querySelector(".page")?.scrollTo({ top: 0, behavior: "smooth" });  // téléphone : la page défile dans son cadre
    },

    // Accueil (nom de l'appli en haut à gauche) : Aujourd'hui.
    home() {
      this.fiche = null;
      return this.openSheet("today");
    },

    openDomains() {
      this.fiche = null;
      if (!this.paged) return;
      this.remember();
      this.showList();
    },

    // Retour arrière du navigateur ou du téléphone : ferme ce qui est ouvert au lieu de quitter l'appli.
    remember() {
      try { history.pushState({ dw: true }, ""); } catch (_) {}
    },

    // Fiche ouverte : elle se ferme ; sinon retour à Aujourd'hui, sans nouvelle entrée d'historique.
    back() {
      if (this.fiche) this.fiche = null;
      else if (!this.todayOpen) this.openSheet("today", false);
    },

    // remember : nouvelle entrée d'historique (false depuis le bouton retour, qui en consomme une).
    async openSheet(name, remember = true) {
      this.fiche = null;
      // L'écran déjà affiché le reste : un second appui ne le ferme pas.
      if (this[`${name}Open`]) return;
      if (remember) this.remember();
      const opening = true;
      this.todayOpen = false;
      this.legendOpen = false;
      this.dropsOpen = false;
      this.exploreOpen = false;
      this.portfolioOpen = false;
      if (name === "today") {
        this.settingsOpen = false;
        this.todayOpen = true;
        await this.loadToday();
      } else if (name === "portfolio") {
        this.settingsOpen = false;
        this.portfolioOpen = true;
        this.decisionsRegrets = !!this._regretsNext;
        this._regretsNext = false;
        await this.loadPortfolio();
      } else if (name === "legend") {
        this.settingsOpen = false;
        this.legendOpen = opening;
      } else if (name === "drops") {
        this.settingsOpen = false;
        this.dropsOpen = opening;
        if (opening) {
          if (this.dropSelected.length) this.dropSelected = [];
          this.dropFocus = this._focusNext || "";
          this._focusNext = "";
          // « Nouveau » : depuis la visite précédente de l'appli ; cette visite devient la référence suivante.
          // Lu une fois par chargement de la page : un aller-retour entre onglets ne change rien (ni redessin).
          if (!this._dropsSeenRead) {
            this._dropsSeenRead = true;
            try {
              this.dropsSeenBefore = localStorage.getItem("drops.seen") || "";
              localStorage.setItem("drops.seen", new Date().toISOString());
            } catch (_) { this.dropsSeenBefore = ""; }
          }
          await this.loadDrops();
        }
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
        // Carte « Sources et lectures » : état des lectures sans la liste des noms.
        if (this.settingsOpen && !this.dropsData) this.loadDrops(true);
      }
    },

    async openSettingsAt(id) {
      await this.openSheet("settings");
      // Carte du bloc avancé : il s'ouvre d'abord.
      if (document.getElementById(id)?.closest(".settings-advanced")) this.settingsAdvanced = true;
      this.$nextTick(() => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }));
    },

    // Aujourd'hui : compteurs, étapes de configuration, les meilleurs noms pour l'usage choisi.
    async loadToday() {
      this.loadOrderUrl();
      try {
        const period = this.todayPeriod;
        const [data, changed] = await this.apiFresh(`/api/today?sort=${this.lens === "exploit" ? "exploit" : "resale"}&period=${period}${this.todayNoPremium ? "&nopremium=1" : ""}`);
        // Période changée pendant le chargement : la réponse ne vaut plus.
        if (period !== this.todayPeriod) return;
        if (!changed && data === this._todayRaw) return;
        this._todayRaw = data;
        this.today = data;
        this.setup = data.setup;
      } catch (e) {
        this.sayError(e, "Aujourd'hui indisponible", () => this.loadToday());
      }
    },

    // Rubrique Pépites d'Aujourd'hui : devenues libres sur la période, puis libération dans la période ; 10 par groupe
    // tant que « Tout afficher » n'est pas demandé (le compteur ouvre tout).
    gemGroups() {
      const p = this.today && this.today.pepites;
      if (!p) return [];
      const span = this.periodLong(this.todayPeriod);
      return [
        { key: "free", title: `Devenues libres depuis ${span}`, items: p.free },
        { key: "coming", title: `Libération sous ${span}`, items: p.coming },
      ].filter((g) => g.items.length).map((g) => ({ ...g, shown: this.gemsAll ? g.items : g.items.slice(0, 10) }));
    },

    showGems() {
      this.gemsAll = true;
      this.$nextTick(() => document.getElementById("today-gems")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    },

    setTodayPeriod(k) {
      if (k === this.todayPeriod) return;
      this.todayPeriod = k;
      this.gemsAll = false;
      try { localStorage.setItem("today.period", k); } catch (_) {}
      return this.loadToday();
    },

    periodLong(k) { return (PERIODS.find((p) => p.k === k) || PERIODS[0]).long; },

    // Puce du chiffre ouvert dans la Veille : « Libérés depuis 7 jours ».
    dropFocusLabel() { return this.dropFocus ? `${DROP_FOCUS[this.dropFocus]} ${this.periodLong(this.dropFocusPeriod)}` : ""; },

    // focus : chiffre d'Aujourd'hui (freed, coming, new_take), la liste montre exactement ses noms, pour la
    // période choisie sur Aujourd'hui.
    async openVeille(tab, focus = "") {
      this.dropFocusPeriod = this.todayPeriod;
      this.dropFocusNoPremium = this.todayNoPremium;
      this.dropSetTab(tab);
      if (this.dropsOpen) { this.dropFocus = focus; await this.loadDrops(); return; }
      // Lu par openSheet à l'ouverture de la Veille, puis effacé.
      this._focusNext = focus;
      await this.openSheet("drops");
      this.dropSetTab(tab);
    },

    async dropClearFocus() {
      this.dropFocus = "";
      this.dropShow = 100;
      this.dropSelected = [];
      await this.loadDrops();
    },

    // Regrets d'Aujourd'hui : les décisions du Portefeuille, filtrées sur les noms ignorés repris.
    async openRegrets() {
      if (this.portfolioOpen) { this.decisionsRegrets = true; await this.loadDecisions(); }
      else { this._regretsNext = true; await this.openSheet("portfolio"); }
      this.$nextTick(() => document.querySelector(".pf-decisions")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    },

    get decisionItems() {
      const items = this.decisions ? this.decisions.items : [];
      return this.decisionsRegrets ? items.filter((d) => d.outcome === "repris") : items;
    },

    plural(n, one, many) { return n > 1 ? many : one; },

    get setupSteps() {
      const go = { sources: () => this.openSettings("fr"), telegram: () => this.openSettings("telegram"), ai: () => this.openSettings("ai") };
      return (this.setup || []).map((step) => ({ ...step, go: go[step.key] }));
    },

    get setupIncomplete() { return this.setupSteps.some((step) => !step.done); },

    // Étapes à jour après un réglage (Telegram associé, source activée, jeton IA créé).
    async refreshMeta() {
      try {
        this.meta = await this.api("GET", "/api/meta");
        this.setup = this.meta.steps || this.setup;
      } catch (_) {}
    },

    // Sommaire de l'Aide : défilement sans toucher à l'adresse (un changement de hash déclenche popstate, qui fermerait l'écran).
    helpGo(href) {
      if (href && href.startsWith("#")) document.getElementById(href.slice(1))?.scrollIntoView({ behavior: "smooth", block: "start" });
    },

    // Bouton « ? » d'un écran : l'Aide, ouverte à la section voulue.
    async helpAt(id) {
      await this.openSheet("legend");
      // Après le fondu de l'écran qui s'en va (150 ms) : avant, le contenu remontait ensuite et l'Aide s'ouvrait
      // plus bas que la section voulue (ordinateur).
      setTimeout(() => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }), 200);
    },

    // « Masquer les noms faibles » : même case dans la Veille et l'Explorer, réglage propre à chaque écran.
    weakHidden(screen) { return screen === "veille" ? this.dropLevel === "good" : this.ex.level === "good"; },

    setWeak(screen, on) {
      if (screen === "veille") { this.dropLevel = on ? "good" : ""; this.dropShow = 40; return; }
      return this.exSet({ level: on ? "good" : "" });
    },

    // « Masquer les premiums » (décoché par défaut : les premiums restent visibles) : Veille, Explorer, Aujourd'hui.
    premiumHidden(screen) {
      if (screen === "veille") return !!this.df.nopremium;
      if (screen === "today") return this.todayNoPremium;
      return this.ex.nopremium === "1";
    },

    setPremiumHidden(screen, on) {
      if (screen === "veille") { this.df.nopremium = on; this.dropShow = 40; return; }
      if (screen === "today") {
        this.todayNoPremium = on;
        try { localStorage.setItem("today.nopremium", on ? "1" : ""); } catch (_) {}
        return this.loadToday();
      }
      return this.exSet({ nopremium: on ? "1" : "" });
    },

    // Gandi : la réponse qui fait l'état affiché (ni une erreur, ni une réponse d'avant un changement d'état).
    gandiOf(i) {
      const g = i && i.gandi;
      return g && !g.stale && !g.pending && g.kind !== "error" ? g : null;
    },

    gandiEuros(p) { return p === null || p === undefined || p === "" ? "" : `${this.fmt(Math.round(Number(p)))}\u00a0€`; },

    // Sorte qui fait l'état affiché, décidée par le serveur pour tous les écrans (explore.gandi_effect) : gandi_state.
    gandiKind(i) {
      return (this.gandiOf(i) && i.gandi_state) || "";
    },

    // Pastille de prix : ambre « Premium 878 € », sinon discrète « 13 € » (prix Gandi, indicatif).
    // short : à côté d'un badge qui dit déjà « Premium (Gandi) » (en-tête de la fiche), le prix seul.
    gandiPill(i, list, short = false) {
      const kind = this.gandiKind(i, list);
      const g = this.gandiOf(i);
      if (!kind || !g) return null;
      const title = `Prix d'achat indicatif : création pour un an, TTC. Vérifié ${this.relative(g.checked_at)}.`;
      if (kind === "premium" && short) return g.price ? { text: this.gandiEuros(g.price), cls: "gandi-premium", title } : null;
      if (kind === "premium") return { text: g.price ? `Premium ${this.gandiEuros(g.price)}` : "Premium", cls: "gandi-premium", title };
      if (kind === "available" && g.price) return { text: this.gandiEuros(g.price), cls: "gandi-price", title };
      return null;
    },

    // Carte de nom : la même dans Aujourd'hui, la Veille, l'Explorer et la recherche (gabarit #name-card).
    // Tout ce que la carte affiche vient d'ici.
    // list : « veille » (élément de /api/drops), « today » (élément de /api/today, même format),
    // « explore » (élément de /api/explore/rank), « pepite » (pépite d'Aujourd'hui, format de l'Explorer), « card »
    // (élément de /api/explore/name) ou « probe » (nom tapé hors des listes, avant et après /api/explore/check).
    nameCard(i, list) {
      const ai = i.ai;
      const lens = this.lens === "exploit" ? "exploitation" : "revente";
      const note = ai ? { label: "IA", value: Number(ai[this.lens]), title: `Note IA de ${lens} sur 100 : la valeur qui trie pour cet usage` } : null;
      const score = i.score === undefined || i.probe ? null : { label: "score", value: i.score, title: this.scoreTitle(i) || `Score ${i.score} sur 100` };
      // La pastille principale est ce qui trie : la note IA de l'usage en cours, sinon le score ; « Score seul » garde le score.
      const scoreFirst = !note || (list === "explore" && this.ex.sort === "score");
      const st = this.cardState(i, list);
      const veilleLike = list === "veille" || list === "today";
      const verdict = this.aiVerdict(ai);
      // Tri « les plus fréquents » : le rang du nom dans sa liste (« 12e prénom le plus donné »).
      const byFreq = (list === "explore" && this.ex.sort === "freq") || (list === "veille" && this.dropSort === "freq");
      const freq = byFreq ? this.freqText(i, list === "explore" ? this.exFreqList() : this.dropFreqList()) : "";
      return {
        name: i.name, list, item: i,
        primary: scoreFirst ? score : note,
        secondary: scoreFirst ? note : score,
        // Verdict en pastille (classe .verdict), le prix et les raisons en texte normal.
        verdict, verdictClass: this.aiClass(ai),
        // Le rang affiché à part (tri par fréquence) n'est pas répété dans les raisons.
        line: ai ? `utilisateur final ${this.priceRange(ai.final_low, ai.final_high)}${ai.yield != null ? " · " + this.yieldText(ai) : ""}` : (i.reasons || []).filter((r) => r !== freq).slice(0, 2).join(" · "),
        freq,
        // Pourquoi « À laisser » ou « À surveiller », en fin de phrase, discret.
        why: ai && ai.why && verdict !== "À prendre" ? ai.why : "",
        ...st,
        isNew: list === "veille" && this.dropNew(i),
        // Prix d'achat chez Gandi en pastille ; premium probable (sans réponse de Gandi) en texte, comme avant.
        // Explorer : l'état d'un premium vu chez Gandi est déjà « Premium 878 € » (badge) ; pas de pastille en double.
        price: (i.status === 8 && !veilleLike) || (veilleLike && this.gandiKind(i, list) === "premium" && ["available", "unconfirmed"].includes(i.state))
          ? null : this.gandiPill(i, list),
        premium: this.gandiKind(i, list) !== "premium" && veilleLike && i.premium ? "premium probable" : "",
        watched: !!i.watched,
        owned: !!i.in_portfolio,
        busy: !!this.exBusy[i.name],
        pickable: list === "veille" || list === "explore",
        picked: list === "veille" ? this.dropSelected.includes(i.name) : list === "explore" && this.exSelected.includes(i.name),
        // « Ignorer » : le nom quitte la Veille, la décision est gardée (Portefeuille, Décisions).
        canIgnore: veilleLike || list === "pepite",
      };
    },

    cardIgnore(c) { return this.ignoreDrops([c.name]); },

    // État explicite d'une carte : badge (texte et couleur), date ou détail, actions possibles.
    cardState(i, list) {
      const g = this.gandiOf(i);
      const checked = i.checked_at ? `vérifié ${this.relative(i.checked_at)}` : g ? `prix vérifié ${this.relative(g.checked_at)}` : "";
      const kind = this.gandiKind(i, list);
      if (list === "veille" || list === "today") {
        // Indisponible chez Gandi : plus jamais « libre » (Veille avec les réservés affichés, Paramètres).
        if (GANDI_BLOCKED.includes(kind)) {
          return { badge: { text: g.label.toLowerCase(), cls: "x-7" }, when: BLOCKED_WHY, canReserve: false, canCheck: false };
        }
        // Premium vu chez Gandi sur un nom libre : même badge que dans l'Explorer et la fiche (« Premium 878 € »).
        if (["available", "unconfirmed"].includes(i.state) && kind === "premium") {
          return { badge: { text: g.price ? `Premium ${this.gandiEuros(g.price)}` : "premium", cls: "x-8 gandi" },
                   when: `prix vérifié ${this.relative(g.checked_at)}`, canReserve: true, canCheck: false };
        }
        if (i.state === "unconfirmed" && kind === "available") {
          return { badge: { text: "libre", cls: "x-5" }, when: `prix vérifié ${this.relative(g.checked_at)}`, canReserve: true, canCheck: false };
        }
        if (i.state === "available") {
          return { badge: { text: "libre", cls: "x-5" }, when: checked || `libéré ${this.relative(i.available_since)}`, canReserve: true, canCheck: false };
        }
        if (i.state === "unconfirmed") {
          return { badge: { text: "libre selon le WHOIS, à confirmer", cls: "x-9" }, when: checked, canReserve: true, canCheck: false };
        }
        return { badge: { text: this.labels[i.state] || i.state, cls: "s-" + i.state },
                 when: i.release_at ? `libération ~${this.shortDate(i.release_at)}` : "libération inconnue", canReserve: false, canCheck: false };
      }
      const r = i.rdap;
      const detail = r && !r.available ? [r.registrar ? `registrar ${r.registrar}` : "", r.expires_at ? `expire le ${this.shortDate(r.expires_at)}` : ""].filter(Boolean).join(", ") : "";
      switch (i.status) {
        case 2: case 3: return { badge: { text: i.status === 3 && DNS_TLDS.includes(i.tld) ? "probablement libre (absent du DNS)" : "probablement libre", cls: "x-3" },
                                 when: checked, canReserve: true, canCheck: true };
        case 5: return { badge: { text: "libre", cls: "x-5" }, when: checked, canReserve: true, canCheck: false };
        // WHOIS seul (.co, .io, .ai) : « inconnu » ne connaît pas les noms réservés ou premium du registre.
        case 9: return { badge: { text: "libre selon le WHOIS, à confirmer", cls: "x-9" }, when: checked, canReserve: true, canCheck: true };
        case 4: return { badge: { text: i.status_label || "Libération à venir", cls: this.badgeClass(i) },
                         when: i.release_at ? `libération ~${this.shortDate(i.release_at)}` : "libération à venir", canReserve: false, canCheck: false };
        case 1: return { badge: { text: "pris", cls: "x-1" }, when: detail || checked, canReserve: false, canCheck: false };
        case 6: return { badge: { text: "pris, suspendu", cls: "x-6" }, when: detail || checked, canReserve: false, canCheck: false };
        // Extension par Gandi seul (.com sans zone) : pris, comme les autres (un seul statut) ; Vérifier lit le registre.
        case 11: return { badge: { text: "pris", cls: "x-1" }, when: g ? `vérifié ${this.relative(g.checked_at)}` : "", canReserve: false, canCheck: true };
        case 7: return GANDI_BLOCKED.includes(kind)
          ? { badge: { text: g.label.toLowerCase(), cls: "x-7" }, when: BLOCKED_WHY, canReserve: false, canCheck: false }
          : { badge: { text: "réservé", cls: "x-7" }, when: (i.reserved || []).join(", "), canReserve: false, canCheck: false };
        // Premium vu chez Gandi : il s'achète, au prix de la pastille ; premium probable : rien n'est confirmé.
        case 8: return kind === "premium"
          ? { badge: { text: g.price ? `Premium ${this.gandiEuros(g.price)}` : "premium", cls: "x-8 gandi" }, when: `prix vérifié ${this.relative(g.checked_at)}`, canReserve: true, canCheck: false }
          : { badge: { text: "premium probable", cls: "x-8" }, when: "", canReserve: false, canCheck: false };
        default: return { badge: { text: DNS_TLDS.includes(i.tld) ? "pas interrogé" : "pas encore vérifié", cls: "x-0" }, when: "", canReserve: false, canCheck: true };
      }
    },

    cardToggle(c) {
      if (c.list === "veille") this.dropToggle(c.name);
      else if (c.list === "explore") this.exToggle(c.name);
    },

    cardWatch(c) { return this.watchName(c.item); },

    async cardCheck(c) {
      const item = await this.verifyName(c.name);
      if (!item) return;
      this.exReplace(item);
      if (c.list === "veille") await this.loadDrops();
      else if (c.list === "today" || c.list === "pepite") await this.loadToday();
      else this.exLoadMap();
    },

    // Explorer : tous les noms suivis, extension par extension.

    // Fiche d'un nom : la même partout (Domaines, Veille, Explorer).

    loadOrderUrl() {
      if (!this.orderUrl) this.api("GET", "/api/settings").then((c) => (this.orderUrl = c.order_url)).catch(() => {});
    },

    // hot : ouverte depuis une notification d'analyse prête ; l'avis IA est mis en avant, une fois.
    async openFiche(name, hot = false) {
      this.loadOrderUrl();
      if (!this.fiche) this.remember();
      const ticket = (this._ficheTicket = (this._ficheTicket || 0) + 1);
      try {
        const fiche = await this.api("GET", `/api/explore/fiche?name=${encodeURIComponent(name)}`);
        // Une autre fiche a été demandée entre-temps : celle-ci est périmée.
        if (ticket !== this._ficheTicket) return;
        fiche.hot = hot;
        // Autre nom : dates clés de nouveau résumées ; le menu « Plus » se referme dans tous les cas.
        if (!this.fiche || this.fiche.name !== fiche.name) this.ficheTlAll = false;
        this.ficheMenu = false;
        this.fiche = fiche;
        // Nom surveillé : l'historique des changements (table history) s'affiche dans la fiche.
        if (fiche.domain_id) this.loadHistory(fiche.domain_id);
        if (hot) this.aiSeen(fiche.name);
      } catch (e) {
        if (ticket === this._ficheTicket) this.sayError(e, name, () => this.openFiche(name, hot));
      }
    },

    // Trait « aujourd'hui » avant la première date à venir (la fiche peut se fermer pendant le rendu).
    timelineNow(n) {
      const line = (this.fiche && this.fiche.timeline) || [];
      return !!(line[n] && line[n].future && (n === 0 || !(line[n - 1] && line[n - 1].future)));
    },

    // Ligne de temps : date seule (AAAA-MM-JJ) ou instant ISO, en court.
    timelineDate(at) {
      return at && at.length <= 10 ? this.shortDate(at + "T12:00:00") : this.shortDate(at);
    },

    decisionLabel(d) { return { ignored: "Ignoré", bought: "Acheté" }[d.decision] || d.decision; },

    // Analyses IA demandées : en attente, et prêtes pas encore ouvertes (bandeau).
    async loadAiRequests() {
      try {
        this.aiReq = await this.api("GET", "/api/ai/requests");
      } catch (_) {}
    },

    async aiSeenAll() {
      const names = this.aiReq.done.map((r) => r.name);
      this.aiReq.done = [];
      this.aiPanel = false;
      for (const name of names) {
        try { this.aiReq = await this.api("POST", "/api/ai/requests/seen", { name }); } catch (_) {}
      }
    },

    // Verdict d'un avis IA, tiré de son commentaire (« À prendre. … ») : pour la cloche.
    aiVerdictOf(comment) {
      const first = (comment || "").split(".")[0].trim();
      return ["À prendre", "À surveiller", "À laisser"].includes(first) ? first : "";
    },

    aiVerdictClass(comment) {
      return this.verdictClass(this.aiVerdictOf(comment));
    },

    async aiSeen(name) {
      this.aiReq.done = this.aiReq.done.filter((r) => r.name !== name);
      try {
        this.aiReq = await this.api("POST", "/api/ai/requests/seen", { name });
      } catch (_) {}
    },

    async ficheAskAi() {
      const name = this.fiche.name;
      try {
        this.fiche.ai_request = await this.api("POST", "/api/ai/request", { name });
        this.loadAiRequests();
        this.say(`Analyse IA demandée pour ${name}. Traitement dans l'heure.`);
      } catch (e) {
        this.sayError(e, name, () => this.ficheAskAi());
      }
    },

    // Portefeuille : noms achetés, renouvellements, mise en vente, ventes ; et les décisions gardées.
    async loadPortfolio() {
      // Rechargement : les vérifications de registre lancées à l'ajout sont parties, la mention s'efface.
      this.pfChecking = [];
      try {
        [this.pf] = await Promise.all([this.api("GET", "/api/portfolio"), this.loadDecisions()]);
      } catch (e) {
        this.sayError(e, "Portefeuille indisponible", () => this.loadPortfolio());
      }
    },

    async loadDecisions() {
      try { this.decisions = await this.api("GET", "/api/decisions"); } catch (_) {}
    },

    async deleteDecision(d) {
      if (!confirm(`Retirer la décision sur ${d.name} ?`)) return;
      try {
        await this.api("DELETE", `/api/decisions/${encodeURIComponent(d.name)}`);
        await this.loadDecisions();
        this.say(`Décision sur ${d.name} retirée.`);
      } catch (e) {
        this.sayError(e, d.name);
      }
    },

    // quiet : pas de message ici (l'appelant fait le sien). Renvoie la réponse, ou null si refusé.
    async pfAdd(name, extra = {}, quiet = false) {
      const body = { ...this.pfNew, ...extra, name: name || this.pfNew.name };
      for (const k of Object.keys(body)) if (body[k] === "") delete body[k];
      try {
        const res = await this.api("POST", "/api/portfolio", body);
        this.pf = res;
        this.pfNew = { name: "", buy_price: "", bought_on: "", renew_price: "" };
        // Vérification RDAP en arrière-plan : l'échéance arrive après ; la mention part au rechargement suivant.
        if (res.checking) this.pfChecking = [...this.pfChecking, body.name.trim().toLowerCase()];
        this.markOwned(body.name.trim().toLowerCase());
        this.loadDecisions();
        if (!quiet) this.say(`${body.name} ajouté au portefeuille.`);
        return res;
      } catch (e) {
        this.sayError(e, body.name);
        return null;
      }
    },

    // Chaque changement de statut ou de prix est confirmé : un champ modifié sans retour laisse un doute.
    async pfSet(item, key, value) {
      try {
        this.pf = await this.api("PUT", `/api/portfolio/${item.id}`, { [key]: value });
        this.say(`Enregistré pour ${item.name} : ${this.pfChange(key, value)}.`);
      } catch (e) {
        this.sayError(e, item.name);
        await this.loadPortfolio();
      }
    },

    // « statut En vente », « prix demandé 900 € », « échéance 06/11/2026 », « notes » : ce qui vient d'être enregistré.
    pfChange(key, value) {
      if (key === "status") return `statut ${(this.pf && this.pf.statuses[value]) || value}`;
      const labels = { buy_price: "prix d'achat", renew_price: "renouvellement", asking_price: "prix demandé", best_offer: "meilleure offre",
        sold_price: "prix de vente", bought_on: "date d'achat", sold_on: "date de vente", expires_at: "échéance", notes: "notes" };
      const label = labels[key] || key;
      if (value === "" || value === null) return `${label} (vide)`;
      if (/prix|offre|renouvellement/.test(label)) return `${label} ${this.euros(value)}`;
      return /date|échéance/.test(label) ? `${label} ${this.shortDate(value)}` : label;
    },

    async pfDelete(item) {
      if (!confirm(`Retirer ${item.name} du portefeuille ?`)) return;
      try {
        this.pf = await this.api("DELETE", `/api/portfolio/${item.id}`);
        this.markOwned(item.name, false);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    euros(n) { return n === null || n === undefined || n === "" ? "" : `${this.fmt(n)} €`; },

    // Carte décision de la fiche, tout l'avis IA : les deux notes (celle de l'usage choisi, réglage commun lens,
    // en premier et en grand), verdict, les deux prix, le pourquoi, le commentaire ; la grille dans « Détail IA ».
    // Sans avis : le score, où en est la notation, les deux premières raisons du score.
    ficheDecision(f) {
      const k = { notes: [], verdict: null, prices: [], why: "", comment: "", queue: "", old: false, grid: null, rated: "", premium: "" };
      if (!f) return k;
      const item = f.item;
      const opinion = f.ai || (item && item.ai);
      const main = this.lens === "exploit" ? "exploit" : "resale";
      if (opinion) {
        const notes = [["resale", "IA revente", "revente"], ["exploit", "IA exploitation", "exploitation"]]
          .map(([key, label, use]) => ({ value: Number(opinion[key]), label, main: key === main,
            title: key === main ? `Note IA de ${use} sur 100 : la valeur qui trie pour cet usage` : `Note IA de ${use} sur 100` }));
        k.notes = notes.sort((a, b) => b.main - a.main);
        k.verdict = { text: this.aiVerdict(opinion), cls: "verdict " + this.aiClass(opinion) };
        k.prices = [["revente rapide", this.priceRange(opinion.quick_low, opinion.quick_high)],
          ["utilisateur final", this.priceRange(opinion.final_low, opinion.final_high)],
          ["rendement", this.yieldText(opinion, true)]].filter(([, v]) => v);
        k.why = opinion.why || "";
        k.comment = (opinion.comment || "").slice(k.verdict.text.length).replace(/^\.\s*/, "");
        k.old = (opinion.version || 0) < 5;
        k.grid = opinion.grid || null;
        k.rated = opinion.rated_at ? `noté le ${this.shortDate(opinion.rated_at)}` : "";
      } else {
        if (item && item.score !== undefined) {
          k.notes = [{ value: item.score, label: "score", main: true, title: this.scoreTitle(item) || `Score ${item.score} sur 100` }];
        }
        k.why = ((item && item.reasons) || []).slice(0, 2).join(" · ");
        const req = f.ai_request;
        const eta = f.ai_eta;
        if (req && !req.done_at) k.queue = `Analyse demandée ${this.relative(req.requested_at)}, traitée dans l'heure`;
        else if (eta && eta.position) k.queue = `Notation la nuit du ${this.shortDate(eta.night)}, ${this.fmt(eta.position)}e de la file`;
        else if (eta) k.queue = `Pas d'avis prévu dans les ${eta.horizon} prochaines nuits : « Analyser » le demande`;
      }
      // Prix d'achat chez Gandi à côté des prix de revente : un premium doit être couvert par la revente.
      const kind = this.gandiKind(item, "explore");
      const g = this.gandiOf(item);
      if (g && g.price && (kind === "premium" || kind === "available")) k.prices.push(["achat", this.gandiEuros(g.price)]);
      if (kind === "premium") {
        k.premium = "Premium : prix d'achat fixé par le registre, bien au-dessus du prix courant. La revente doit le couvrir.";
      } else if (GANDI_BLOCKED.includes(kind)) {
        k.premium = `${g.label} : ${BLOCKED_WHY}.`;
      } else if (item && item.status === 8) {
        k.premium = "Premium probable : le registre peut demander bien plus que le prix courant, Gandi le dira.";
      }
      return k;
    },

    // Détail IA : les neuf sous-notes sur 10 d'où viennent les notes et le verdict ; un risque de 5 ou plus est signalé.
    ficheGridHtml(g) {
      if (!g) return "";
      return `<ul class="fgrid">${AI_GRID.map(([key, label]) => {
        const v = Math.max(0, Math.min(10, Number(g[key]) || 0));
        return `<li class="${key === "risque" && v >= 5 ? "warn" : ""}"><span class="fg-label">${label}</span>`
          + `<span class="fg-bar" aria-hidden="true"><i style="width:${v * 10}%"></i></span><b>${v}</b></li>`;
      }).join("")}</ul>`;
    },

    // Nom de la fiche : coupure permise avant l'extension, jamais au milieu sans besoin.
    ficheNameHtml(name) {
      const esc = String(name || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      return esc.replace(".", "<wbr>.");
    },

    // Badge d'état de l'en-tête : l'état de l'Explorer, sinon celui du suivi (nom hors des listes).
    ficheBadge(f) {
      if (f && f.item) return { text: f.item.status_label, cls: this.badgeClass(f.item), title: "" };
      const d = this.ficheDomain;
      if (d) return { text: this.stateLabel(d), cls: this.stateClass(d), title: this.stateTitle(d) };
      return { text: "Hors des listes suivies", cls: "x-0", title: "" };
    },

    // La date qui compte, à côté de l'état : libre depuis, libération estimée, ou expiration.
    ficheWhen(f) {
      if (!f) return "";
      const tl = f.timeline || [];
      const item = f.item || {};
      const d = this.ficheDomain;
      const day = (at) => {
        const when = new Date(at && at.length <= 10 ? at + "T12:00:00" : at);
        // Sous dix mois, jour et mois suffisent ; au-delà, l'année aussi.
        return Math.abs(when - this.now) < 300 * 864e5 ? when.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) : this.shortDate(when);
      };
      if (item.status === 9 || (d && d.state === "available" && d.source === "whois")) return "à confirmer chez un registrar";
      // Indisponible à l'achat ou réservé : pas « libre depuis », même si le suivi le voit libre au registre.
      if ((item.status === 5 || (d && d.state === "available")) && item.status !== 7) {
        const seen = [...tl].reverse().find((t) => !t.future && t.kind === "available");
        const since = item.available_since || (seen && seen.at);
        return since ? `libre depuis le ${day(since)}` : "";
      }
      const release = tl.find((t) => t.future && t.kind === "release");
      if (release) return `libération ~${day(release.at)}`;
      const expires = tl.find((t) => t.future && t.kind === "expires");
      return expires ? `expire le ${this.timelineDate(expires.at)}` : "";
    },

    // Une date de ce genre figure déjà dans Dates clés : la section Suivi ne la répète pas.
    ficheHas(f, kind) { return !!(f && (f.timeline || []).some((t) => t.kind === kind)); },

    // Dates clés : au-delà de 6, les 3 dernières passées et toutes celles à venir (« Tout afficher » pour le reste).
    // n : rang dans la liste complète (trait « aujourd'hui » de timelineNow).
    ficheTimeline(f) {
      const all = ((f && f.timeline) || []).map((t, n) => ({ ...t, n }));
      if (all.length <= 6 || this.ficheTlAll) return { items: all, hidden: 0 };
      const items = [...all.filter((t) => !t.future).slice(-3), ...all.filter((t) => t.future)];
      return { items, hidden: all.length - items.length };
    },

    // Menu « Plus » : le premier choix prend le clavier ; Échap, Tab ou un clic dehors le ferment.
    ficheMenuToggle() {
      this.ficheMenu = !this.ficheMenu;
      // Après l'affichage du menu (x-show), sinon ses boutons sont encore masqués.
      if (this.ficheMenu) this.$nextTick(() => requestAnimationFrame(() => this.ficheMenuItems()[0]?.focus()));
    },

    ficheMenuItems() {
      return [...document.querySelectorAll("#fiche-menu [role=menuitem]")].filter((b) => b.offsetParent && !b.disabled);
    },

    ficheMenuClose(refocus = false) {
      this.ficheMenu = false;
      if (refocus) document.querySelector(".fiche-acts .fa-more")?.focus();
    },

    ficheMenuKey(e) {
      const items = this.ficheMenuItems();
      const at = items.indexOf(document.activeElement);
      const go = (i) => { e.preventDefault(); items[(i + items.length) % items.length]?.focus(); };
      if (e.key === "Escape") { e.stopPropagation(); this.ficheMenuClose(true); }
      else if (e.key === "ArrowDown") go(at + 1);
      else if (e.key === "ArrowUp") go(at - 1);
      else if (e.key === "Home") go(0);
      else if (e.key === "End") go(items.length - 1);
      else if (e.key === "Tab") this.ficheMenu = false;
    },

    // Action mise en avant : réserver un nom libre, surveiller un nom qui va se libérer, sinon faire noter.
    fichePrimary(f) {
      const s = f.item ? f.item.status : null;
      if ([2, 3, 5, 9].includes(s)) return "reserve";
      if (s === 4 && !f.domain_id) return "watch";
      if (!(f.ai || (f.item && f.item.ai))) return "ai";
      return "";
    },

    // État de l'avis IA d'une fiche : aucun, demandé (en cours), fait.
    ficheAiState(f) {
      const r = f.ai_request;
      if (r && !r.done_at) return "pending";
      return f.ai || (f.item && f.item.ai) ? "done" : "none";
    },

    // Picto IA : analyser, ou relancer un avis déjà là (après confirmation, l'avis du jour suffit souvent).
    ficheAi() {
      const state = this.ficheAiState(this.fiche);
      if (state === "pending") return;
      if (state === "done" && !confirm(`Relancer l'analyse IA de ${this.fiche.name} ? L'avis actuel sera remplacé.`)) return;
      return this.ficheAskAi();
    },

    aiAskLabel(f) {
      const r = f.ai_request;
      if (r && !r.done_at) return "Analyse IA demandée";
      return f.ai || (f.item && f.item.ai) ? "Relancer l'analyse IA" : "Analyse IA";
    },

    async ficheReload() {
      if (this.fiche) await this.openFiche(this.fiche.name);
    },

    // Vérifier : pour un nom surveillé, le check de Domaines (état, historique) ; sinon une lecture du registre.
    async ficheCheck() {
      const d = this.ficheDomain;
      if (d) {
        await this.check(d, true);
      } else {
        const item = await this.verifyName(this.fiche.name);
        if (!item) return;
        this.exReplace(item);
      }
      await Promise.all([this.ficheReload(), this.refreshUnderFiche()]);
    },

    // Écran resté ouvert sous la fiche (Aujourd'hui, Veille, carte de l'Explorer) : relu après une action de la
    // fiche, pour que ses cartes disent le nouvel état. Les autres se relisent à leur ouverture.
    async refreshUnderFiche() {
      const jobs = [];
      if (this.todayOpen) jobs.push(this.loadToday());
      if (this.dropsOpen) jobs.push(this.loadDrops());
      if (this.exploreOpen) jobs.push(this.exLoadMap());
      await Promise.all(jobs);
    },

    async ficheWatch() {
      const name = this.fiche.name;
      this.newName = name;
      await this.add();
      if (this.domains.some((x) => x.name === name)) this.markWatched(name);
      await Promise.all([this.ficheReload(), this.refreshUnderFiche()]);
    },

    ficheBusy() {
      const d = this.ficheDomain;
      return !!this.exBusy[this.fiche.name] || !!(d && this.busy[d.id]);
    },

    async ficheUnwatch() {
      const d = this.ficheDomain;
      if (d && await this.remove(d)) {
        this.markWatched(d.name, false);
        await Promise.all([this.ficheReload(), this.refreshUnderFiche()]);
      }
    },

    // Un nom passe sous surveillance (ou en sort) : les listes déjà affichées le montrent.
    markWatched(name, on = true) { this.markItems(name, { watched: on }); },

    // Nom ajouté au portefeuille (ou retiré) : badge « Portefeuille » aussitôt, sur toutes les cartes affichées.
    markOwned(name, on = true) { this.markItems(name, { in_portfolio: on }); },

    markItems(name, patch) {
      this.exReplace({ name, ...patch });
      for (const list of [(this.dropsData && this.dropsData.items) || [], (this.today && this.today.items) || []]) {
        for (const d of list) if (d.name === name) Object.assign(d, patch);
      }
    },

    async ficheSeo() {
      await this.exSeoOne({ name: this.fiche.name });
      await this.ficheReload();
    },

    async ficheFlag() {
      const name = this.fiche.name;
      const reserved = this.fiche.item.status !== 7;
      try {
        await this.api("POST", "/api/reserved/flag", { name, reserved });
      } catch (e) {
        this.say(`${name} : ${e.message}`, "error");
        return;
      }
      this.say(reserved ? `${name} marqué réservé par le registre.` : `${name} remis dans la liste.`);
      await this.ficheReload();
      // Marqué : il quitte la liste des noms à saisir ; remis : sa ligne prend le nouvel état.
      if (reserved) this.exItems = this.exItems.filter((x) => x.name !== name);
      else if (this.fiche && this.fiche.item) this.exReplace(this.fiche.item);
    },

    // « Ajouter aux noms suivis » (recherche de l'Explorer, fiche d'un nom hors des listes) : le libellé entre
    // dans Mes ajouts ; le nom demandé (avec extension) est vérifié au registre dans la foulée.
    mineAddable(label) {
      const folded = String(label || "").toLowerCase().replace(/œ/g, "oe").replace(/æ/g, "ae").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
      return LABEL_OK.test(folded);
    },

    async mineAdd(name) {
      if (this.mineBusy) return;
      this.mineBusy = true;
      try {
        const r = await this.api("POST", "/api/explore/mine", { name });
        const check = r.check_error ? ` Vérification : ${r.check_error}` : "";
        this.say(`${r.label} ajouté aux noms suivis (Mes ajouts). Les zones le chercheront à leur prochaine lecture ; d'ici là, « Vérifier » pour chaque extension.${check}`);
        await this.mineRefresh();
      } catch (e) {
        this.sayError(e, name, () => this.mineAdd(name));
      } finally {
        this.mineBusy = false;
      }
    },

    async mineRemove() {
      const label = this.fiche && this.fiche.label;
      if (!label || this.mineBusy || !confirm(`Retirer ${label} de Mes ajouts ? Il n'est plus suivi s'il n'est dans aucune autre liste.`)) return;
      this.mineBusy = true;
      try {
        await this.api("DELETE", `/api/explore/mine/${encodeURIComponent(label)}`);
        this.say(`${label} retiré de Mes ajouts.`);
        await this.mineRefresh();
      } catch (e) {
        this.sayError(e, label, () => this.mineRemove());
      } finally {
        this.mineBusy = false;
      }
    },

    // Univers changé : la fiche, la carte de recherche et la liste de l'Explorer sont relues.
    async mineRefresh() {
      const jobs = [this.ficheReload()];
      if (this.exploreOpen) jobs.push(this.ex.q.trim() ? this.exTyped() : this.exLoad(), this.exLoadMap());
      await Promise.all(jobs);
    },

    // Nom tapé dans la recherche : avec l'extension tapée, sinon la première de l'Explorer (vérifiée à l'ajout).
    mineSearchName() {
      const q = this.ex.q.trim().toLowerCase();
      const typed = q.split(".").slice(1).join(".");
      const tld = typed || (this.exTlds()[0] || "");
      return this.exCard.label + (tld ? "." + tld : "");
    },

    // Fiche d'un nom : lignes libellé / valeur de chaque section (valeurs déjà échappées), sans champ vide.
    ficheParts(f) {
      const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      const score = [], name = [], past = [];
      if (!f) return { score, name, past };
      const item = f.item;
      if (item && item.score !== undefined) {
        score.push(["Total", `<span class="ex-bar fiche-bar"><i style="width:${Number(item.score)}%"></i><b>${Number(item.score)}</b></span>`]);
        const detail = this.scoreTitle(item).replace(/^Score \d+ : /, "");
        if (detail) score.push(["Détail", esc(detail)]);
        if (item.reasons && item.reasons.length) score.push(["Raisons", `<span class="fchips">${item.reasons.map((r) => `<span class="ex-reason">${esc(r)}</span>`).join("")}</span>`]);
      }
      const kinds = (this.exData && this.exData.kinds) || (this.dropsData && this.dropsData.kinds) || {};
      if (f.categories && f.categories.length) name.push(["Catégories", esc(f.categories.map((c) => this.kindLabel(kinds, c)).join(", "))]);
      if (f.natures && f.natures.length) name.push(["Nature", esc(f.natures.map((n) => NATURE_LABELS[n] || n).join(", "))]);
      // Rang dans chaque liste, du mieux placé au moins bien placé : « 442e mot anglais le plus courant ».
      const ranks = (item && item.ranks) || {};
      const ranked = Object.keys(ranks).sort((a, b) => ranks[a] - ranks[b]).map((k) => this.freqText(item, k));
      if (ranked.length) name.push(["Fréquence", esc(ranked.join(", "))]);
      else if (f.rank) name.push(["Fréquence", `n° ${esc(this.fmt(f.rank))} de sa liste`]);
      // Libération estimée et prochaine lecture du registre : déjà dans Dates clés.
      if (item && item.created_on) past.push(["Open data .fr", esc(`créé le ${this.shortDate(item.created_on)}` + (item.removed_on ? `, supprimé le ${this.shortDate(item.removed_on)}` : ""))]);
      if (item && item.checked_at) past.push(["Vérifié", esc(this.relative(item.checked_at))]);
      if (item && item.next_check_at && !f.rdap_next && !this.ficheHas(f, "next_check")) past.push(["Prochaine vérification", esc(this.relative(item.next_check_at))]);
      if (f.dns_eta) {
        const d = f.dns_eta;
        past.push(["DNS .fr, prochaine", esc(d.queued ? `prochaine passe, lancée à minuit (${this.fmt(d.position)}e sur ${this.fmt(d.nightly)} au plus)` : `pas à la prochaine passe : ${d.why}`)]);
      }
      if (f.dns) past.push(["DNS .fr, dernière", esc((f.dns.present ? "délégué : nom pris" : "aucun serveur : probablement libre") + `, ${this.relative(f.dns.at)}`)]);
      else if (f.tld === "fr") past.push(["DNS .fr, dernière", "jamais interrogé"]);
      if (f.cc_dns) {
        const c = f.cc_dns;
        const lost = !c.present && c.lost_at ? `, sorti du DNS ${this.relative(c.lost_at)} : à vérifier` : "";
        past.push([`DNS .${f.tld}, dernière`, esc((c.present ? "délégué : nom pris" : "aucun serveur") + lost + `, ${this.relative(c.at)}`)]);
      } else if (DNS_TLDS.includes(f.tld)) past.push([`DNS .${f.tld}`, "pas dans les noms suivis chaque nuit"]);
      if (f.gandi) past.push(["Prix d'achat", esc(this.gandiFicheText(f.gandi))]);
      if (f.seo) {
        const parts = [];
        if (f.seo.pr) parts.push(`PageRank ${String(f.seo.pr).replace(".", ",")}`);
        if (f.seo.wb_first) parts.push(`archivé depuis ${f.seo.wb_first.slice(0, 4)}` + (f.seo.wb_years ? ` (${f.seo.wb_years} ans)` : ""));
        past.push(["SEO", esc(parts.join(", ") || "aucun signal") + `, analysé ${esc(this.relative(f.seo.checked_at))}`]);
      }
      return { score, name, past };
    },

    // Registre et SEO, ligne Gandi : statut, prix TTC (et HT), date ; erreur ou réponse d'avant un changement d'état.
    gandiFicheText(g) {
      const kinds = { available: "libre au prix standard", premium: "premium", unavailable: "indisponible", reserved: "réservé par le registre",
        restricted: "restreint (conditions d'éligibilité)", error: "sans réponse exploitable" };
      const money = (v) => Number(v).toLocaleString("fr-FR", { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + "\u00a0€";
      const price = g.price !== null && g.price !== undefined
        ? `, ${money(g.price)} TTC` + (g.price_ht !== null && g.price_ht !== undefined ? ` (${money(g.price_ht)} HT)` : "") + " pour un an, indicatif" : "";
      const parts = [`${kinds[g.kind] || g.kind}${price}`, `vérifié ${this.relative(g.checked_at)}`];
      if (g.error && g.kind !== "error") parts.push(`dernière vérification en erreur : ${g.error}`);
      else if (g.error) parts.push(g.error);
      if (g.stale) parts.push("avant le dernier changement d'état, revu à la prochaine passe");
      if (g.pending) parts.push("sans doute enregistré depuis, revérification au registre en cours");
      return parts.join(", ");
    },

    // Lignes libellé / valeur (valeurs déjà échappées) : libellés en colonne étroite, au-dessus sous 360 px.
    ficheDl(rows) {
      if (!rows || !rows.length) return "";
      return `<dl class="fdl">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
    },

    // Résumés des en-têtes de section, d'un coup d'œil.
    ficheNameSum(f) {
      if (!f || !f.categories) return "";
      const kinds = (this.exData && this.exData.kinds) || (this.dropsData && this.dropsData.kinds) || {};
      const ranks = (f.item && f.item.ranks) || {};
      // Les listes où le nom est le mieux placé d'abord ; les formes (lettres, chiffres) seulement à défaut.
      const lists = f.categories.filter((c) => !SHAPE_KINDS.includes(c)).sort((a, b) => (ranks[a] || 1e9) - (ranks[b] || 1e9));
      return (lists.length ? lists : f.categories).slice(0, 2).map((c) => this.kindLabel(kinds, c)).join(", ");
    },

    ficheCompSum(f) {
      const n = ((f && f.comparables) || []).length + ((f && f.references) || []).length + ((f && f.sales) || []).length;
      return `${n} nom${n > 1 ? "s" : ""}`;
    },

    ficheOthersSum(f) {
      const all = (f && f.others) || [];
      const free = all.filter((o) => [2, 3, 5, 9].includes(o.status)).length;
      return `${free ? free : "aucune"} libre${free > 1 ? "s" : ""} sur ${all.length}`;
    },

    // Registre et SEO : dernière lecture RDAP ou WHOIS (sauf nom surveillé : Suivi la donne), open data, DNS, SEO.
    ficheRegistryHtml(f) {
      if (!f) return "";
      const reg = this.ficheDomain ? "" : this.rdapDl(f.rdap, f.rdap_next);
      const past = this.ficheParts(f).past.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
      return reg || past ? `<dl class="fdl">${reg}${past}</dl>` : "";
    },

    ficheRegistrySum(f) {
      if (!f) return "";
      const d = this.ficheDomain;
      if (f.rdap && !d) return `${f.rdap.source === "whois" ? "WHOIS" : "RDAP"}, lu ${this.relative(f.rdap.at)}`;
      if (d && d.last_checked_at) return `${d.source ? d.source.toUpperCase() + ", " : ""}lu ${this.relative(d.last_checked_at)}`;
      if (f.dns) return `DNS ${this.relative(f.dns.at)}`;
      if (f.item && f.item.checked_at) return `vérifié ${this.relative(f.item.checked_at)}`;
      return f.item && f.item.created_on ? "open data .fr" : "pas encore lu";
    },

    // Badge d'un nom : l'état précis de la légende (couleurs de l'écran Domaines) quand il se libère.
    badgeClass(i) {
      if (!i) return "x-0";
      if (i.status === 8 && this.gandiKind(i, "explore") === "premium") return "x-8 gandi";
      if (i.status === 11) return "x-1";  // pris d'après Gandi : même badge que pris
      return i.status === 4 && i.drop_state ? "s-" + i.drop_state : "x-" + i.status;
    },

    // Fourchette de prix d'un avis IA : « 2 000 à 6 000 € », « 0 € » quand rien.
    // Rendement attendu (avis récents) : chance de vente réelle × prix bas, commission et renouvellement ôtés.
    yieldText(o, long) {
      if (!o || o.yield == null) return "";
      const v = Math.round(Math.abs(o.yield));
      const head = o.yield >= 0 ? `rapporte ~${v}\u00a0€/an` : `coûte ~${v}\u00a0€/an`;
      if (!long || !o.sell_rate) return head;
      const pct = (o.sell_rate * 100).toLocaleString("fr-FR", { maximumFractionDigits: 1 });
      const years = o.median_years != null ? `, 1 chance sur 2 en ~${Math.round(o.median_years)}\u00a0ans` : "";
      return `${head} (vente ~${pct}\u00a0%/an${years})`;
    },

    priceRange(low, high) {
      if (!high) return "0 €";
      const n = (v) => Number(v).toLocaleString("fr-FR");
      // Espace insécable avant « € » : le symbole ne passe jamais seul à la ligne.
      return low !== high ? `${n(low || 0)} à ${n(high)}\u00a0€` : `${n(high)}\u00a0€`;
    },

    // Étiquette de la colonne Valeur : verdict, puis prix utilisateur final (même formule que les cartes).
    aiTag(a) {
      if (!a) return "";
      return `${this.aiVerdict(a)} · utilisateur final ${this.priceRange(a.final_low, a.final_high)}`;
    },

    aiVerdict(a) { return a && a.comment ? a.comment.split(".")[0] : ""; },

    aiClass(a) { return this.verdictClass(this.aiVerdict(a)); },

    verdictClass(v) { return VERDICT_CLASSES[v] || ""; },

    // « 12e prénom le plus donné » : rang dans la liste key si le nom y est, sinon dans la liste choisie par le
    // serveur (freq_list), sinon dans celle où son rang est le meilleur.
    freqText(i, key) {
      const r = (i && i.ranks) || {};
      const k = key && r[key] ? key : i.freq_list && r[i.freq_list] ? i.freq_list : Object.keys(r).sort((a, b) => r[a] - r[b])[0];
      if (!k) return "";
      const first = ["form", "combo", "trend", "expr"].includes(k) ? "1re" : "1er";
      return `${r[k] === 1 ? first : this.fmt(r[k]) + "e"} ${FREQ_PHRASES[k] || "de la liste " + this.kindLabel(null, k)}`;
    },

    async loadAiStatus() {
      try { this.aiStatus = await this.api("GET", "/api/ai/status"); } catch (_) {}
    },

    // Dernière lecture du registre et prochaine lecture : lignes libellé / valeur de la section Registre et SEO.
    rdapDl(r, next) {
      const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      // Jamais lu : le résumé de la section le dit, pas de ligne vide.
      let body = r ? this.rdapHtml(r) : "";
      if (next) {
        // La date est aussi dans Dates clés : ici, surtout la raison.
        const when = next.at ? `${this.shortDate(next.at)}, ${next.why}` : `non prévue : ${next.why}`;
        body += `<dt>Prochaine lecture</dt><dd>${esc(when)}</dd>`;
      }
      return body;
    },

    // Compteurs d'état exacts seulement sans niveau ni score minimum : ils comptent des masques, pas
    // des scores (les calculer pour chaque nom coûterait des secondes à chaque clic).
    exCountsExact() { return !this.ex.level && !(this.ex.min && this.ex.min !== "0"); },

    // Bouton « 50 de plus » : liste pleine, ou compteur exact de l'état plus grand que la liste.
    get exHasMore() {
      if (this.exNoMore || !this.exData) return false;
      return this.exItems.length >= this.exLimit || (this.exCountsExact() && (this.exData.counts?.[this.ex.status] || 0) > this.exItems.length);
    },

    exQuery(cursor) {
      const e = this.ex;
      const params = new URLSearchParams({ q: e.q.trim(), mode: "prefix", status: e.status, cat: e.cat, len: e.len, nat: e.nat, rank: e.rank, taken: e.taken, min: e.min && e.min !== "0" ? e.min : "", junk: e.junk, seo: e.seo, lang: e.lang || "", nopremium: e.nopremium || "", gprice: e.gprice || "", level: e.level || "", cursor: String(cursor) });
      if (this.ex.tld) params.set("tld", this.ex.tld);
      for (const k of Object.keys(AI_FILTER_DEFAULTS)) if (String(e[k] ?? "").trim() !== "") params.set(k, String(e[k]).trim());
      return params.toString();
    },

    // more : « 50 de plus » ; sinon rechargement en place (filtre changé, rafraîchissement après une action).
    async exLoad(reset = true, more = false) {
      if (reset) { this.exLimit = 50; this.exNoMore = false; this.exSelected = []; }
      const before = this.exItems.length;
      const ticket = (this._exTicket = (this._exTicket || 0) + 1);
      this.exLoading = true;
      try {
        const [data, changed] = await this.apiFresh(`/api/explore/rank?${this.exQuery(-1)}&limit=${this.exLimit}&sort=${this.exSortParam()}`);
        // Une frappe ou un filtre plus récent a déjà relancé le chargement : ce résultat est périmé.
        if (ticket !== this._exTicket) return;
        // Même liste que celle affichée (retour sur l'écran) : rien à redessiner.
        if (!changed && data === this._exRaw) return;
        this._exRaw = data;
        this.exError = "";
        this.exData = data;
        this.ex.tld = data.tld;
        this.exItems = data.items;
        // « 50 de plus » sans rien de nouveau : fin de la liste pour ces filtres.
        if (more && data.items.length <= before) this.exNoMore = true;
      } catch (e) {
        if (ticket !== this._exTicket) return;
        // Extension mémorisée qui n'existe plus : retour à l'extension par défaut, une fois.
        if (e.status === 404 && this.ex.tld) {
          this.ex.tld = "";
          this.ex.taken = "";
          this.exLoading = false;
          return this.exLoad(reset, more);
        }
        this.exError = e.message;
      } finally {
        if (ticket === this._exTicket) this.exLoading = false;
      }
    },

    async exLoadMap() {
      try {
        const [data, changed] = await this.apiFresh("/api/explore/map");
        if (changed || !this.exMap) this.exMap = data;
      } catch (_) {
        this.exMap = null;
      }
    },

    async exTyped() {
      const q = this.ex.q.trim();
      const loading = this.exLoad();
      if (!q) { this.exCard = null; this.exProbe = []; return loading; }
      try {
        const card = await this.api("GET", `/api/explore/name?label=${encodeURIComponent(q)}`);
        if (this.ex.q.trim() === q) {
          this.exCard = card;
          this.exProbe = card.known ? [] : this.probeItems(q, card.label);
          this.$nextTick(() => document.querySelector(".ex-card")?.scrollIntoView({ behavior: "smooth", block: "start" }));
        }
      } catch (_) {
        this.exCard = null;
        this.exProbe = [];
      }
      return loading;
    },

    // Nom tapé absent des listes : une ligne par extension suivie (ou celle tapée, « monprojet.fr »),
    // vérifiable une à une. Un nom impossible (espace, accent) ne donne rien.
    probeItems(q, label) {
      if (!label || !/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(label) || !this.exData) return [];
      const typed = q.toLowerCase().split(".").slice(1).join(".");
      const tlds = typed && this.exData.tlds.includes(typed) ? [typed] : this.exData.tlds;
      const kept = Object.fromEntries(this.exProbe.map((p) => [p.name, p]));
      return tlds.map((t) => kept[`${label}.${t}`] || { name: `${label}.${t}`, label, tld: t, probe: true });
    },

    async exMapPick(key, tld) {
      const f = this.exMap.filters[key];
      // Les cases comptent ce que la puce « Libres » liste (réservables) : la liste s'ouvre dessus, la carte se referme.
      this.exMapOpen = false;
      await this.exSet({ tld, cat: f.cat || "", len: f.len ? String(f.len) : "", status: "bookable" });
      this.$nextTick(() => document.querySelector(".ex-list-head")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    },

    // Filtres : un seul point d'entrée, mémorisé pour la prochaine ouverture.
    async exSet(patch) {
      Object.assign(this.ex, patch);
      // « Libération la plus proche » ne vaut que pour Bientôt libres : ailleurs, retour aux meilleurs.
      if (this.ex.status !== "coming" && this.ex.sort === "release") this.ex.sort = "best";
      // Mélanges et tirets ne sont jamais « prononçables » : sans les suites incluses, la liste serait vide.
      if ("cat" in patch && this.exCats().some((c) => c === "mixed" || c === "hyphen")) this.ex.junk = "1";
      // « Les plus donnés en … » n'existe qu'avec le type Prénoms.
      if ("cat" in patch && patch.cat !== "firstname" && this.ex.sort === "recent") this.ex.sort = "best";
      this.exSelected = [];
      try { localStorage.setItem("explorer", JSON.stringify({ ...this.ex, q: "", v: 2 })); } catch (_) {}
      if (this.exTopOpen) this.exLoadTop();
      await this.exLoad();
    },

    exRestore() {
      try {
        const saved = JSON.parse(localStorage.getItem("explorer") || "null");
        // Réglages d'avant la refonte : on repart des réglages simples, en gardant les extensions.
        if (saved && saved.v === 2) Object.assign(this.ex, this.exNormalize(saved), { q: "" });
        else if (saved) this.ex.tld = saved.tld || "";
      } catch (_) {}
    },

    // Réglages venus d'ailleurs (vue enregistrée, carte, mémoire du navigateur) : état, tri et niveau
    // ramenés aux réglages simples de l'écran (anciens « available », « seo », « gem »…).
    exNormalize(filters) {
      // premium : ancien réglage (premiums probables affichés) ; ils le sont par défaut, nopremium les retire.
      const { v, premium, ...f } = filters || {};
      if (f.status && !EX_STATES.some((s) => s.k === f.status)) f.status = ["available", "maybe_free", "free"].includes(f.status) ? "bookable" : "all";
      if (f.sort && !EX_SORTS.includes(f.sort)) f.sort = "best";
      if (f.sort === "release" && f.status && f.status !== "coming") f.sort = "best";
      if (f.level && f.level !== "good") f.level = "good";
      // Plusieurs catégories : dans l'ordre du serveur (triées), pour reconnaître la vue active. Secteurs porteurs
      // n'a plus de puce : ses noms sont dans Termes stars (CAT_ALIASES, wordlists.ALIASES côté serveur).
      if (f.cat) f.cat = [...new Set(String(f.cat).split(",").filter(Boolean).map((c) => CAT_ALIASES[c] || c))].sort().join(",");
      return f;
    },

    exActive() {
      if (!this.exData) return [];
      const e = this.ex;
      const out = [];
      for (const c of this.exCats()) if (!EX_TYPES.some((t) => t.k === c)) out.push({ key: "cat:" + c, label: this.exData.kinds[c] || c });
      if (e.nat) out.push({ key: "nat", label: this.exData.natures[e.nat] });
      if (e.lang) out.push({ key: "lang", label: this.exData.langs[e.lang] });
      if (e.len && e.len !== "-4") out.push({ key: "len", label: this.exLengths.find((o) => o.v === e.len)?.label + " car." });
      if (e.rank) out.push({ key: "rank", label: this.exRanks.find((o) => o.v === e.rank)?.label });
      if (e.taken) out.push({ key: "taken", label: e.taken === "any" ? "Pris ailleurs" : "Pris en ." + e.taken });
      if (e.min && e.min !== "0") out.push({ key: "min", label: "Score ≥ " + e.min });
      if (e.junk) out.push({ key: "junk", label: "Suites et mélanges inclus" });
      if (e.seo) out.push({ key: "seo", label: this.exData.seo_filters[e.seo] });
      if (String(e.gprice ?? "").trim() !== "") out.push({ key: "gprice", label: `Achat ≤ ${this.fmt(Number(e.gprice))} €` });
      if (e.ai_v) out.push({ key: "ai_v", label: "IA : " + this.exAiVerdicts().map((v) => this.exData.ai_verdicts[v]).join(", ") });
      for (const f of Object.keys(this.exData.ai_fields || {})) {
        const lo = String(e[`ai_${f}_min`] ?? "").trim(), hi = String(e[`ai_${f}_max`] ?? "").trim();
        if (lo === "" && hi === "") continue;
        const name = this.exData.ai_fields[f];
        const text = lo !== "" && hi !== "" ? `${lo} à ${hi}` : lo !== "" ? `≥ ${lo}` : `≤ ${hi}`;
        out.push({ key: [`ai_${f}_min`, `ai_${f}_max`], label: `IA ${name.toLowerCase()} ${text}` });
      }
      // Réglages simples (en-tête sur ordinateur, panneau Avancé sur téléphone) : ils cachent aussi des noms.
      if (this.ex.level === "good") out.push({ key: "level", label: "Noms faibles masqués" });
      if (this.ex.nopremium === "1") out.push({ key: "nopremium", label: "Premiums masqués" });
      return out;
    },

    exClear(key) {
      if (typeof key === "string" && key.startsWith("cat:")) return this.exSet({ cat: this.exCats().filter((c) => c !== key.slice(4)).join(",") });
      return this.exSet(Object.fromEntries([].concat(key).map((k) => [k, ""])));
    },

    // Filtres du panneau Avancé seulement : l'état, le tri, le niveau et l'usage restent.
    exReset() { return this.exSet({ cat: "", len: "", nat: "", lang: "", rank: "", taken: "", min: "", junk: "", seo: "", gprice: "", ...AI_FILTER_DEFAULTS }); },

    exSortParam() { return this.ex.sort === "best" ? this.lens : this.ex.sort; },

    // Catégories cochées, plusieurs à la fois (union) : cat=firstname,surname.
    exCats() { return (this.ex.cat || "").split(",").filter(Boolean); },

    // Rang affiché avec le tri « Les plus fréquents » : une seule liste cochée, son rang ; sinon freq_list du serveur.
    exFreqList() {
      const lists = this.exCats().filter((k) => !SHAPE_KINDS.includes(k));
      return lists.length === 1 ? lists[0] : "";
    },

    // Puce de catégorie cochée ou non ; elle s'ajoute aux autres ou se retire.
    exToggleCat(k) {
      const on = this.exCats();
      const next = on.includes(k) ? on.filter((x) => x !== k) : [...on, k];
      return this.exSet({ cat: next.sort().join(",") });
    },

    exTypeOn(t) {
      if (t.k === "short") return this.ex.len === "-4" && !this.ex.cat;
      if (!t.k) return !this.ex.cat && this.ex.len !== "-4";
      return this.exCats().includes(t.k) && this.ex.len !== "-4";
    },

    // Un type ne touche pas à la longueur choisie dans Avancé ; seul « Courts (1 à 4) » est une longueur.
    // « Toutes » efface les types ; les autres se cochent à plusieurs.
    exSetType(t) {
      if (t.k === "short") return this.exSet({ cat: "", len: "-4" });
      const len = this.ex.len === "-4" ? "" : this.ex.len;
      if (!t.k) return this.exSet({ cat: "", len });
      const on = this.ex.len === "-4" ? [] : this.exCats();
      const next = on.includes(t.k) ? on.filter((x) => x !== t.k) : [...on, t.k];
      return this.exSet({ cat: next.sort().join(","), len });
    },

    setLens(k) {
      this.lens = k;
      try { localStorage.setItem("lens", k); } catch (_) {}
      if (this.exploreOpen && this.ex.sort === "best") this.exLoad();
      if (this.todayOpen) this.loadToday();
    },

    exAiVerdicts() { return (this.ex.ai_v || "").split(",").filter(Boolean); },

    exToggleVerdict(k) {
      const on = this.exAiVerdicts();
      const next = on.includes(k) ? on.filter((x) => x !== k) : [...on, k];
      return this.exSet({ ai_v: next.join(",") });
    },

    async exLoadViews() {
      try { this.exViews = await this.api("GET", "/api/explore/views"); } catch (_) {}
    },

    async exSaveViews(views) {
      try {
        this.exViews = await this.api("PUT", "/api/explore/views", { views });
        return true;
      } catch (e) {
        this.say(e.message, "error");
        return false;
      }
    },

    async exSaveView() {
      const name = prompt("Nom de la vue", this.exActive().map((a) => a.label).join(", "));
      if (!name) return;
      // Filtres posés seulement : le serveur limite leur nombre. Une vue réenregistrée garde son réglage d'alerte.
      const filters = Object.fromEntries(Object.entries(this.ex).filter(([k, val]) => !["q", "v"].includes(k) && String(val ?? "") !== ""));
      const before = this.exViews.find((v) => v.name === name);
      if (await this.exSaveViews([...this.exViews.filter((v) => v.name !== name), { name, filters, alert: !!(before && before.alert) }])) this.say(`Vue « ${name} » enregistrée.`);
    },

    // Case « alerte » d'une vue : Telegram signale ses nouveaux noms libres, une fois par heure au plus.
    async exSetViewAlert(n, on) {
      const views = this.exViews.map((v, i) => (i === n ? { ...v, alert: on } : v));
      if (await this.exSaveViews(views)) this.say(on ? `Vue « ${views[n].name} » : alerte Telegram activée.` : `Vue « ${views[n].name} » : alerte retirée.`);
    },

    // Filtres complets d'une vue : les absents sont vierges, les anciens réglages ramenés aux réglages simples.
    exViewFilters(v) { return { ...EX_BLANK, ...this.exNormalize(v.filters) }; },

    exApplyView(v) { return this.exSet({ ...this.exViewFilters(v), q: "" }); },

    exToggleTop() {
      this.exTopOpen = !this.exTopOpen;
      if (this.exTopOpen) this.exLoadTop();
    },

    // Listes du Top : celles de l'Explorer, plus « Prénoms <année> » quand le fichier de l'Insee a été lu.
    topLists() {
      const recent = this.exData && this.exData.recent;
      return TOP_LISTS.filter((k) => this.exData.kinds[k] || (recent && k === recent.key));
    },

    topLabel(k) {
      const recent = this.exData && this.exData.recent;
      return recent && k === recent.key ? recent.label : this.kindLabel(this.exData.kinds, k);
    },

    async exLoadTop() {
      this.topData = null;
      try {
        const params = new URLSearchParams({ cat: this.top.cat, n: String(this.top.n), tld: this.exTlds().join(","), lang: this.ex.lang || "" });
        this.topData = await this.api("GET", `/api/explore/top?${params}`);
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    // Vue active : tous ses filtres sont ceux du moment.
    exViewOn(v) {
      return Object.entries(this.exViewFilters(v)).every(([k, val]) => String(this.ex[k] ?? "") === String(val ?? ""));
    },

    async exDeleteView(n) {
      const v = this.exViews[n];
      if (!v || !confirm(`Supprimer la vue « ${v.name} » ? Les filtres affichés restent en place.`)) return;
      if (await this.exSaveViews(this.exViews.filter((_, i) => i !== n))) this.say(`Vue « ${v.name} » supprimée.`);
    },

    exTlds() { return (this.ex.tld || "").split(",").filter(Boolean); },

    // Plusieurs extensions à la fois ; il en reste toujours une. Depuis « Toutes », une puce choisit
    // cette extension seule (sinon il faudrait décocher toutes les autres une à une).
    exToggleTld(t) {
      const on = this.exTlds();
      const all = this.exData && this.exData.tlds.length > 1 && on.length === this.exData.tlds.length;
      const next = all ? [t] : on.includes(t) ? on.filter((x) => x !== t) : [...on, t];
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
        // Bilan en fin de lot (« 3 vérifiés, 1 échec ») plutôt qu'un message par nom.
        let ok = 0, fail = 0;
        for (const i of items) {
          try {
            if (action === "check") {
              const item = await this.verifyName(i.name, true);
              if (item) { this.exReplace(item); ok += 1; } else fail += 1;
            } else if (action === "watch") {
              if (i.watched || await this.watchName(i, true)) ok += 1; else fail += 1;
            } else if (action === "reserve") {
              await this.api("POST", "/api/reserved/flag", { name: i.name, reserved: true });
              ok += 1;
            }
          } catch (_) {
            fail += 1;
          }
          this.exBulkDone += 1;
        }
        if (action === "check") { this.exLoadMap(); this.say(tally(ok, fail, "vérifié", "vérifiés"), fail ? "error" : "info"); }
        if (action === "watch") this.say(tally(ok, fail, "surveillé", "surveillés"), fail ? "error" : "info");
        if (action === "reserve") { await this.exLoad(false); this.exLoadMap(); this.say(tally(ok, fail, "marqué réservé", "marqués réservés"), fail ? "error" : "info"); }
        this.exSelected = [];
      } catch (e) {
        this.sayError(e);
      } finally {
        this.exBulkRunning = false;
      }
    },

    scoreTitle(i) {
      if (!i.parts) return "";
      const names = { shape: "forme et sonorité", popularity: "sens et marché", demand: "demande", history: "historique", seo: "SEO" };
      return `Score ${i.score} : ` + Object.entries(i.parts).filter(([, v]) => v).map(([k, v]) => `${names[k]} ${v}`).join(", ");
    },

    rdapHtml(r) {
      const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      // Libre d'après le seul WHOIS : à confirmer (noms réservés ou premium du registre inconnus du WHOIS).
      const state = r.available ? (r.source === "whois" ? "inconnu du WHOIS, libre à confirmer" : "libre") : "enregistré";
      const rows = [[r.source === "whois" ? "WHOIS" : "RDAP", `${state}, lu ${this.relative(r.at)}`]];
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

    prenomsLine(p) {
      if (!p.latest) return p.at ? `Pas encore de données, dernier essai ${this.relative(p.at)}.` : "Pas encore téléchargé : premier essai quelques minutes après le démarrage.";
      const missing = p.untracked ? `, ${this.fmt(p.untracked)} hors des listes suivies (${p.untracked_examples.join(", ")}…), non suivis` : "";
      return `Année ${p.latest} : ${this.fmt(p.names)} prénoms les plus donnés, ${this.fmt(p.tracked)} suivis${missing}. Lu ${this.relative(p.at)}.`;
    },

    async refreshPrenoms() {
      try {
        await this.api("POST", "/api/prenoms/refresh");
        this.cfg.prenoms.running = true;
        const poll = setInterval(async () => {
          const st = await this.settingsStatus();
          if (!st || st.prenoms.running) return;
          clearInterval(poll);
          await this.loadSettings();
        }, 3000);
      } catch (e) {
        this.sayError(e, "", () => this.refreshPrenoms());
      }
    },

    async savePrenomsUrl() {
      try {
        this.cfg.prenoms = await this.api("PUT", "/api/settings/prenoms", { url: this.cfg.prenoms.url || "" });
        this.say("Adresse enregistrée. « Mettre à jour » pour lire le fichier.");
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
        // Sondage léger (/api/settings/status) ; les compteurs complets sont relus une fois à la fin.
        const poll = setInterval(async () => {
          const st = await this.settingsStatus();
          if (!st || st.reserved.running) return;
          clearInterval(poll);
          await this.loadSettings();
        }, 3000);
      } catch (e) {
        this.sayError(e, "", () => this.refreshReserved());
      }
    },

    async settingsStatus() {
      try { return await this.api("GET", "/api/settings/status"); } catch (_) { return null; }
    },

    async exSeoStatus() {
      try { this.exSeo = await this.api("GET", "/api/explore/seo"); } catch (_) {}
    },

    async exSeoOne(i) {
      this.exBusy[i.name] = true;
      try {
        await this.api("POST", "/api/explore/seo", { names: [i.name] });
        // Depuis une fiche ouverte ailleurs, la liste de l'Explorer n'est pas affichée : rien à recharger.
        if (this.exploreOpen) {
          await this.exLoad(false);
          if (this.exCard) await this.exTyped();
        }
        this.say(`${i.name} : analyse SEO faite.`);
      } catch (e) {
        this.sayError(e, i.name, () => this.exSeoOne(i));
      } finally {
        this.exBusy[i.name] = false;
      }
    },

    async saveGandiKey() {
      try {
        this.cfg.gandi = { ...this.cfg.gandi, ...(await this.api("PUT", "/api/settings/gandi", { key: this.gandiKey })) };
        this.gandiKey = "";
        this.gandiResults = [];
        this.say(this.cfg.gandi.key_set ? "Jeton Gandi enregistré. « Tester » vérifie qu'il marche." : "Jeton Gandi retiré.");
        // Passes de nuit et continue : extensions concernées et état changent avec le jeton.
        await this.loadSettings();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async testGandi() {
      this.gandiTesting = true;
      try {
        const found = await this.api("POST", "/api/settings/gandi/test");
        this.gandiResults = found.results;
        this.cfg.gandi = { ...this.cfg.gandi, key_set: found.key_set, tested_at: found.tested_at, error: found.error };
        this.say(found.ok ? "Gandi répond." : "Gandi n'a pas répondu, voir le détail.", found.ok ? "" : "error");
      } catch (e) {
        this.sayError(e, "Test Gandi impossible", () => this.testGandi());
      }
      this.gandiTesting = false;
    },

    // Ligne de résultat du test : état et prix de création tels que Gandi les donne.
    gandiLine(r) {
      if (r.error) return r.error;
      const price = r.price !== null && r.price !== undefined ? ` · ${this.fmt(Math.round(r.price))} € TTC${r.type ? ` (${r.type})` : ""}` : "";
      return `${r.status}${price}`;
    },

    // Passe de nuit Gandi : activation, rythme, plafond, délai avant de revoir un nom.
    async saveGandiPass(patch) {
      try {
        const pass = await this.api("PUT", "/api/settings/gandi/pass", patch);
        this.cfg.gandi = { ...this.cfg.gandi, pass };
        this.say("Passe Gandi enregistrée.");
      } catch (e) {
        this.say(e.message, "error");
        await this.loadSettings();
      }
    },

    // « Lancer maintenant » : la passe tout de suite, jusqu'au plafond ; l'état est relu jusqu'à la fin.
    async runGandi() {
      this.gandiStarting = true;
      try {
        const found = await this.api("POST", "/api/settings/gandi/run");
        this.say(found.started ? "Passe Gandi lancée." : "Une passe Gandi est déjà en cours.");
        const poll = setInterval(async () => {
          await this.loadSettings();
          if (!this.cfg || !this.cfg.gandi.pass || !this.cfg.gandi.pass.running) clearInterval(poll);
        }, 5000);
        await this.loadSettings();
      } catch (e) {
        this.sayError(e, "Passe Gandi impossible", () => this.runGandi());
      }
      this.gandiStarting = false;
    },

    // Passe continue des extensions par Gandi seul : activation (décochée, elle s'arrête), rythme, délai.
    async saveGandiLive(patch) {
      try {
        const live = await this.api("PUT", "/api/settings/gandi/live", patch);
        this.cfg.gandi = { ...this.cfg.gandi, live };
        this.say(patch.enabled === false ? "Passe continue arrêtée." : "Passe continue enregistrée.");
      } catch (e) {
        this.say(e.message, "error");
        await this.loadSettings();
      }
    },

    // Passe continue, en une phrase : extensions, noms vérifiés, restant, rythme, prochain tour, dernier arrêt.
    gandiLiveLine(d) {
      if (!d) return "";
      const tlds = (d.tlds || []).map((t) => "." + t).join(", ");
      if (!tlds) return (d.configured || []).length ? "aucune extension sans zone : la passe de nuit s'en charge." : "aucune extension.";
      const parts = [`${tlds} : ${this.fmt(d.verified || 0)} nom(s) vérifié(s)`];
      if (d.remaining !== null && d.remaining !== undefined) parts.push(`${this.fmt(d.remaining)} à demander ou à revoir`);
      parts.push(`${String(d.rate).replace(".", ",")} requête(s) par seconde`);
      if (d.enabled === false) parts.push("passe arrêtée");
      else if (d.halted_at) parts.push(`arrêtée : ${d.error || "jeton refusé"}, nouvel essai demain ou avec un nouveau jeton`);
      else if (d.running && d.next_tour) parts.push(`tout est à jour, prochain tour ${this.relative(d.next_tour)}`);
      else if (d.running) parts.push("en cours");
      else if (d.stopped) parts.push(`arrêtée : ${d.stopped}, reprise sous 5 minutes`);
      else parts.push("démarrage sous 5 minutes");
      return parts.join(" ; ") + ".";
    },

    // Dernière passe Gandi, en une phrase : Paramètres et Sources et lectures.
    gandiPassLine(d) {
      if (!d) return "";
      if (d.running) {
        const c = d.current || {};
        return c.queued ? `passe en cours : ${this.fmt(c.checked || 0)} nom(s) vérifié(s) sur ${this.fmt(c.queued)}.` : "passe en cours.";
      }
      if (d.enabled === false) return "passe de nuit désactivée.";
      const t = d.totals || {};
      const known = t.known ? `, ${this.fmt(t.known)} nom(s) connus de Gandi en tout` : "";
      if (!d.finished_at) return `première passe cette nuit, entre ${this.gandiWindow(d)}${known}.`;
      const k = d.kinds || {};
      const split = [["available", "libre(s) au prix standard"], ["premium", "premium(s)"], ["unavailable", "indisponible(s)"],
        ["reserved", "réservé(s)"], ["restricted", "restreint(s)"], ["error", "sans réponse exploitable"]]
        .filter(([key]) => k[key]).map(([key, label]) => `${this.fmt(k[key])} ${label}`);
      const parts = [`dernière passe le ${this.exact(d.finished_at)} : ${this.fmt(d.checked || 0)} nom(s) vérifié(s) sur ${this.fmt(d.queued || 0)}`];
      if (split.length) parts.push(split.join(", "));
      if (d.stopped === "heure de fin atteinte") parts.push("arrêtée à la fin de la fenêtre, la suite la nuit prochaine");
      else if (d.stopped) parts.push(`arrêtée : ${d.stopped}`);
      return parts.join(" ; ") + known + ".";
    },

    gandiWindow(d) {
      const w = (d && d.window) || "03:30-04:45";
      return w.replace(/^0?(\d+):(\d+)-0?(\d+):(\d+)$/, "$1 h $2 et $3 h $4");
    },

    // Domaines : prix d'achat d'un domaine non pris, ou « Indisponible chez Gandi ».
    domGandi(d) {
      const g = this.gandiOf(d);
      const kind = this.gandiKind(d);
      if (!g || !kind) return null;
      const title = `Prix d'achat indicatif : création pour un an, TTC. Vérifié ${this.relative(g.checked_at)}.`;
      if (kind === "premium") return { text: g.price ? `Premium ${this.gandiEuros(g.price)}` : "Premium", cls: "gandi-premium", title };
      if (kind === "available" && g.price) return { text: `achat ${this.gandiEuros(g.price)}`, cls: "gandi-price", title };
      return null;
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

    exReplace(item) {
      for (const list of [this.exItems, this.exCard ? this.exCard.items : [], this.exProbe]) {
        const at = list.findIndex((x) => x.name === item.name);
        if (at >= 0) list[at] = { ...list[at], ...item, probe: list[at].probe };
      }
    },

    // quiet : pas de message (action groupée, le bilan vient à la fin).
    async verifyName(name, quiet = false) {
      this.exBusy[name] = true;
      try {
        const item = await this.api("POST", "/api/explore/check", { name });
        // Repli sur le WHOIS (RDAP en panne) : dit dans le message, avec le détail.
        const g = this.gandiOf(item);
        const gandi = item.gandi_note ? " Prix d'achat non vérifié." : g && g.price && ["available", "premium"].includes(g.kind) ? ` Achat ${this.gandiEuros(g.price)}.` : "";
        if (!quiet) this.say(`${name} : ${item.status_label || "vérifié"}.` + (item.note ? " Lu dans le WHOIS, RDAP indisponible." : "") + gandi,
          "info", null, [item.note, item.gandi_note].filter(Boolean).join(" ; "));
        return item;
      } catch (e) {
        if (!quiet) this.sayError(e, name, () => this.verifyName(name));
        return null;
      } finally {
        this.exBusy[name] = false;
      }
    },

    async exCheck(i) { return this.cardCheck({ name: i.name, list: "explore" }); },

    // Surveiller depuis une carte (Veille, Explorer, recherche) : ajouté à Domaines, cartes mises à jour.
    async watchName(i, quiet = false) {
      this.newName = i.name;
      const d = await this.add(quiet);
      if (this.domains.some((x) => x.name === i.name)) this.markWatched(i.name);
      return !!d;
    },

    // Tri en masse dans la Veille : sélection, puis une action pour tous.
    dropToggle(name) {
      this.dropSelected = this.dropSelected.includes(name) ? this.dropSelected.filter((n) => n !== name) : [...this.dropSelected, name];
    },

    dropShown() { return this.dropList.slice(0, this.dropShow); },

    dropAllPicked() {
      const shown = this.dropShown();
      return shown.length > 0 && shown.every((d) => this.dropSelected.includes(d.name));
    },

    dropPickAll(on) {
      const names = this.dropShown().map((d) => d.name);
      const rest = this.dropSelected.filter((n) => !names.includes(n));
      this.dropSelected = on ? [...rest, ...names] : rest;
    },

    async dropBulk(action) {
      const names = [...this.dropSelected];
      const items = this.dropsData.items.filter((d) => names.includes(d.name));
      this.dropBulkRunning = true;
      this.dropBulkDone = 0;
      try {
        if (action === "hide") {
          await this.ignoreDrops(names);
        } else {
          // Bilan en fin de lot (« 3 vérifiés, 1 échec ») plutôt qu'un message par nom.
          let ok = 0, fail = 0;
          for (const d of items) {
            try {
              if (action === "watch") { if (d.watched || await this.watchName(d, true)) ok += 1; else fail += 1; }
              else if (action === "ai") { await this.api("POST", "/api/ai/request", { name: d.name }); ok += 1; }
              else if (action === "check") { if (await this.verifyName(d.name, true)) ok += 1; else fail += 1; }
            } catch (_) {
              fail += 1;
            }
            this.dropBulkDone += 1;
          }
          const kind = fail ? "error" : "info";
          if (action === "ai") { this.loadAiRequests(); this.say(tally(ok, fail, "analyse IA demandée", "analyses IA demandées") + (ok ? ". Traitement dans l'heure." : ""), kind); }
          if (action === "watch") this.say(tally(ok, fail, "surveillé", "surveillés"), kind);
          if (action === "check") { await this.loadDrops(); this.say(tally(ok, fail, "vérifié", "vérifiés"), kind); }
        }
        this.dropSelected = [];
      } catch (e) {
        this.sayError(e);
      } finally {
        this.dropBulkRunning = false;
      }
    },

    // « Ignorer » (un nom ou une sélection) : le nom quitte la Veille, la décision est gardée ;
    // le message propose d'annuler (POST /api/drops/unhide avec les noms).
    async ignoreDrops(names) {
      try {
        const r = await this.api("POST", "/api/drops/hide", { names });
        if (this.dropsData) {
          // Nouvel objet : modifier sur place abîmait la réponse gardée en cache (apiFresh), et « Annuler »
          // (304, même réponse) ne réaffichait pas le nom.
          this.dropsData = { ...this.dropsData, items: (this.dropsData.items || []).filter((d) => !names.includes(d.name)), hidden: r.hidden };
          this._dropsRaw = null;
          this._dropsVer = (this._dropsVer || 0) + 1;
        }
        if (this.today) {
          this.today.items = this.today.items.filter((d) => !names.includes(d.name));
          // Un nom ignoré n'est plus une pépite : la rubrique et son compteur le perdent ensemble.
          const gems = this.today.pepites;
          if (gems) {
            gems.free = gems.free.filter((d) => !names.includes(d.name));
            gems.coming = gems.coming.filter((d) => !names.includes(d.name));
            this.today.counts.pepites = gems.free.length + gems.coming.length;
          }
        }
        this.dropSelected = this.dropSelected.filter((n) => !names.includes(n));
        this._dropsVer = (this._dropsVer || 0) + 1;
        this.say(names.length > 1 ? `${names.length} noms ignorés.` : `${names[0]} ignoré.`, "info", { label: "Annuler", run: () => this.unhideDrops(names) });
      } catch (e) {
        this.sayError(e, "", () => this.ignoreDrops(names));
      }
    },

    // Sans noms : tout réafficher (Paramètres) ; avec : ces noms seulement (bouton Annuler).
    async unhideDrops(names = null) {
      try {
        await this.api("POST", "/api/drops/unhide", names ? { names } : undefined);
        await this.loadDrops();
        if (this.todayOpen) await this.loadToday();
        this.say(names ? (names.length > 1 ? `${names.length} noms réaffichés.` : `${names[0]} réaffiché.`) : "Noms ignorés réaffichés.");
      } catch (e) {
        this.sayError(e, "", () => this.unhideDrops(names));
      }
    },

    // Domaines : sélection multiple, puis Vérifier ou Analyse IA pour tous, bilan en un message.
    domToggle(id) {
      this.domSelected = this.domSelected.includes(id) ? this.domSelected.filter((x) => x !== id) : [...this.domSelected, id];
    },

    domSelectAll() {
      this.domSelected = this.domSelected.length ? [] : this.rows.map((d) => d.id);
    },

    async domBulk(action) {
      const items = this.domains.filter((d) => this.domSelected.includes(d.id));
      this.domBulkRunning = true;
      this.domBulkDone = 0;
      let ok = 0, fail = 0;
      for (const d of items) {
        try {
          if (action === "check") {
            const updated = await this.api("POST", `/api/domains/${d.id}/check`);
            this.replace(updated);
            if (updated.check_error) fail += 1; else ok += 1;
          } else if (action === "ai") {
            await this.api("POST", "/api/ai/request", { name: d.name });
            ok += 1;
          }
        } catch (_) {
          fail += 1;
        }
        this.domBulkDone += 1;
      }
      this.now = Date.now();
      if (action === "ai") this.loadAiRequests();
      this.say(action === "check" ? tally(ok, fail, "vérifié", "vérifiés") : tally(ok, fail, "analyse IA demandée", "analyses IA demandées") + (ok ? ". Traitement dans l'heure." : ""),
        fail ? "error" : "info");
      this.domSelected = [];
      this.domBulkRunning = false;
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

    // .co, .ai, .io : noms interrogés, présents et absents à la dernière passe, avec sa date ; puis les totaux.
    // Passe DNS en cours : où elle en est, en direct (status().current).
    dnsLive(d) {
      const c = d.current;
      if (!c) return "passe en cours.";
      const checked = c.checked || 0, present = c.present || 0;
      const parts = [`passe en cours depuis ${this.relative(c.started_at).replace(/^il y a /, "")} : ${this.fmt(checked)} nom(s) interrogé(s)`
        + (c.queued ? ` sur ${this.fmt(c.queued)} prévus` : ""), `${this.fmt(present)} présent(s), ${this.fmt(checked - present)} absent(s)`];
      if (c.skipped) parts.push(`${this.fmt(c.skipped)} sans réponse`);
      if (c.rate) parts.push(`rythme ${this.perSecond(c.rate)} par seconde`);
      return parts.join(", ") + ".";
    },

    ccDnsLine(d) {
      if (!d || !d.enabled) return "désactivé.";
      if (d.running) return this.dnsLive(d);
      if (d.error) return `erreur à la dernière passe : ${d.error}`;
      if (!d.finished_at) return "première passe à minuit, étalée sur la journée.";
      const checked = d.checked || 0;
      const present = d.present || 0;
      const parts = [`dernière passe le ${this.exact(d.finished_at)} : ${this.fmt(checked)} nom(s) interrogé(s), ${this.fmt(present)} présent(s), ${this.fmt(checked - present)} absent(s)`];
      const t = d.totals;
      if (t && t.known !== checked) parts.push(`en tout ${this.fmt(t.known)} interrogé(s), ${this.fmt(t.present)} présent(s), ${this.fmt(t.absent)} absent(s)`);
      if (t && t.lost) parts.push(`${this.fmt(t.lost)} sorti(s) du DNS, suivi(s) dans la Veille`);
      if (d.skipped) parts.push(`${this.fmt(d.skipped)} sans réponse`);
      if (d.stopped === "heure de fin atteinte") parts.push("arrêtée à 23 h, la suite le lendemain");
      else if (d.stopped) parts.push(`arrêtée : ${d.stopped}`);
      return parts.join(", ") + ".";
    },

    frDns(d) {
      if (!d || !d.enabled) return "désactivé.";
      if (d.running) return this.dnsLive(d);
      if (d.error) return `erreur à la dernière passe : ${d.error}`;
      if (!d.finished_at) return "première passe à minuit, étalée sur la journée.";
      const parts = [`dernière passe ${this.relative(d.finished_at)} : ${this.fmt(d.checked)} nom(s) interrogé(s)` + (d.queued ? ` sur ${this.fmt(d.queued)} prévus` : "")];
      parts.push(`${this.fmt(d.present || 0)} avec serveurs (pris)`);
      parts.push(`${this.fmt(d.newly_taken)} nouvellement pris`);
      if (d.skipped) {
        const why = Object.entries(d.failures || {}).map(([r, n]) => `${this.fmt(n)} ${r}`).join(", ");
        parts.push(`${this.fmt(d.skipped)} sans réponse` + (why ? ` (${why})` : ""));
      }
      if (d.rate) parts.push(`rythme final ${this.perSecond(d.rate)} par seconde`);
      if (d.stopped === "heure de fin atteinte") parts.push("arrêtée à 23 h, la suite le lendemain");
      else if (d.stopped) parts.push(`arrêtée : ${d.stopped}`);
      if (d.next_cap && d.next_cap < 10) parts.push(`plafond la nuit prochaine ${this.perSecond(d.next_cap)} par seconde`);
      return parts.join(", ") + ".";
    },

    perSecond(n) {
      return String(n).replace(".", ",");
    },

    frTomorrow(f) {
      const t = f.work.tomorrow;
      const total = t.anniversaries + t.removals;
      return total ? `${this.fmt(total)} prévue(s) : ${this.fmt(t.anniversaries)} date(s) anniversaire, ${this.fmt(t.removals)} suppression(s).` : "rien de prévu.";
    },

    async saveDropsSettings() {
      try {
        this.cfg.drops = await this.api("PUT", "/api/settings/drops", { keep_days: this.cfg.drops.keep_days, min_score: Number(this.cfg.drops.min_score) || 0 });
        this.say("Réglages de la Veille enregistrés.");
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    // Veille : noms courts, mots et noms de famille sortis des zones ICANN.

    // statusOnly : état des lectures seul (rafraîchissement de l'écran ouvert), la liste reste en place.
    async loadDrops(statusOnly = false) {
      this.loadOrderUrl();
      try {
        if (statusOnly) {
          const { items, ...status } = await this.api("GET", "/api/drops?items=0");
          if (this.dropsData) Object.assign(this.dropsData, status);
          else this.dropsData = { ...status, items: [] };
          return;
        }
        const focus = this.dropFocus, period = this.dropFocusPeriod;
        const [data, changed] = await this.apiFresh(focus ? `/api/drops?focus=${focus}&period=${period}${this.dropFocusNoPremium ? "&nopremium=1" : ""}` : "/api/drops");
        // Focus changé pendant le chargement : la réponse ne vaut plus.
        if (focus !== this.dropFocus || (focus && period !== this.dropFocusPeriod)) return;
        // Même réponse que celle affichée (retour sur l'écran) : rien à redessiner.
        if (!changed && data === this._dropsRaw) return;
        this._dropsRaw = data;
        this.dropsData = data;
        this._dropsVer = (this._dropsVer || 0) + 1;
      } catch (e) {
        if (!statusOnly) this.sayError(e, "Veille indisponible", () => this.loadDrops());
      }
    },

    // Listes triées gardées tant que ni les données ni les filtres ne changent : 3 000 noms ne sont
    // plus filtrés et triés à chaque rafraîchissement de l'affichage.
    dropsMemo(kind, build) {
      const key = JSON.stringify([kind, this._dropsVer, this.df, this.dropTld, this.dropKinds, this.dropsSeenBefore,
        this.dropTab, this.dropNewOnly, this.dropLevel, this.dropSort, this.lens, this.dropFocus, Math.floor(this.now / 600000)]);
      this._memo = this._memo || {};
      if (this._memo[kind]?.key !== key) this._memo[kind] = { key, list: build() };
      return this._memo[kind].list;
    },

    dropMatches(d) {
      const f = this.df;
      const len = f.len ? Number(f.len) : 0;
      const label = d.name.slice(0, -(d.tld.length + 1));
      const q = f.q.trim().toLowerCase();
      return (!this.dropTld.length || this.dropTld.includes(d.tld))
        && (!q || d.name.includes(q))
        && (!this.dropKinds.length || d.categories.some((c) => this.dropKinds.includes(c)))
        && (!f.ai.length || f.ai.includes(this.aiClass(d.ai)))
        && (!f.quick || (d.ai && d.ai.quick_low >= Number(f.quick)))
        && (!f.nat || (d.natures || []).includes(f.nat))
        && (!f.lang || (d.langs || []).includes(f.lang))
        && (!len || (len < 0 ? label.length <= -len : len >= 5 ? label.length >= 5 : label.length === len))
        && (!f.rank || (d.rank && d.rank <= Number(f.rank)))
        && (!this.dropMin || d.score >= this.dropMin)
        && (f.junk || d.good !== false)
        && (!f.nopremium || !d.premium)
        && (f.gprice === "" || f.gprice === null || f.gprice === undefined || this.dropGandiPrice(d) <= Number(f.gprice))
        && this.dropPeriodOk(d);
    },

    // Prix d'achat chez Gandi d'un nom de la Veille (libre au prix standard ou premium), sinon l'infini : le filtre
    // « prix d'achat au plus » ne garde que les noms vérifiés chez Gandi.
    dropGandiPrice(d) {
      const g = this.gandiOf(d);
      return g && g.price !== null && ["available", "premium"].includes(d.gandi_state) ? Number(g.price) : Infinity;
    },

    // Période de la Veille : Libérés, libres depuis N ; Bientôt libres, libération estimée dans les N prochaines.
    dropPeriodOk(d) {
      const p = PERIODS.find((x) => x.k === this.df.period);
      if (!p) return true;
      const span = p.hours * 3600 * 1000;
      if (["available", "unconfirmed"].includes(d.state)) {
        const at = Date.parse(d.available_since || d.state_since || "");
        return at >= this.now - span;
      }
      const at = Date.parse(d.release_at || "");
      return at <= this.now + span;
    },

    // Score minimum explicite (filtres avancés) ; les niveaux font le tri courant.
    get dropMin() {
      return Number(this.df.min) || 0;
    },

    // « Masquer les noms faibles », comme dans l'Explorer : avec un avis IA, ce qu'il dit ; sans avis, score de 30.
    dropLevelOk(d) {
      if (!this.dropLevel) return true;
      const v = this.aiVerdict(d.ai);
      if (v) return v !== "À laisser";
      return d.score >= 30;
    },

    // Pour revendre / pour exploiter : conseillés par l'IA d'abord, puis sans avis, puis déconseillés.
    dropLensTier(d) { return !d.ai ? 1 : this.aiVerdict(d.ai) === "À laisser" ? 0 : 2; },

    dropLensValue(d) { return d.ai ? d.ai[this.lens] : d.score; },

    dropInTab(d, tab) {
      return tab === "free" ? ["available", "unconfirmed"].includes(d.state) : ["expired", "redemption", "pending_delete"].includes(d.state);
    },

    dropTabCount(tab) {
      if (!this.dropsData) return 0;
      return this.dropsMemo("count:" + tab, () => this.dropsData.items.filter((d) => this.dropInTab(d, tab) && this.dropMatches(d) && this.dropLevelOk(d)).length);
    },

    dropNewCount() {
      if (!this.dropsData) return 0;
      return this.dropsMemo("new", () => this.dropsData.items.filter((d) => this.dropInTab(d, this.dropTab) && this.dropMatches(d) && this.dropLevelOk(d) && this.dropNew(d)).length);
    },

    get dropList() {
      if (!this.dropsData) return [];
      return this.dropsMemo("list", () => {
        // Chiffre d'Aujourd'hui : tous ses noms, sans onglet ni filtre (même compte que sur Aujourd'hui).
        const list = this.dropFocus ? [...this.dropsData.items] : this.dropsData.items.filter((d) => this.dropInTab(d, this.dropTab) && this.dropMatches(d) && this.dropLevelOk(d)
          && (!this.dropNewOnly || this.dropNew(d)));
        const best = (a, b) => this.dropLensTier(b) - this.dropLensTier(a) || this.dropLensValue(b) - this.dropLensValue(a) || b.score - a.score;
        const sort = this.dropSort;
        if (sort === "recent") {
          const at = (d) => (d.state === "available" ? d.available_since : d.state_since) || "";
          return list.sort((a, b) => at(b).localeCompare(at(a)));
        }
        if (sort === "release") return list.sort((a, b) => (a.release_at || "9").localeCompare(b.release_at || "9"));
        if (sort === "freq") {
          // Du plus donné au moins donné (Insee pour prénoms et noms) ; les noms hors des listes à la fin.
          const key = this.dropFreqList();
          const rank = (d) => { const r = d.ranks || {}; return key ? r[key] ?? Infinity : Math.min(Infinity, ...Object.values(r)); };
          return list.sort((a, b) => rank(a) - rank(b) || best(a, b));
        }
        return list.sort(best);
      });
    },

    // Tri appliqué : le choix de l'utilisateur, sauf « Libération la plus proche » hors de Bientôt libres
    // (les meilleurs d'abord) ; ce choix revient de lui-même au retour sur Bientôt libres.
    get dropSort() { return this.dropSortMode === "release" && this.dropTab !== "coming" ? "best" : this.dropSortMode; },

    // Une seule liste cochée dans Avancé (Prénoms, Noms de famille…) : le rang se lit dans celle-ci.
    dropFreqList() {
      const lists = this.dropKinds.filter((k) => !SHAPE_KINDS.includes(k));
      return lists.length === 1 ? lists[0] : "";
    },

    dropSetTab(tab) {
      this.dropTab = tab;
      this.dropShow = 40;
      this.dropSelected = [];
    },

    // « Nouveaux depuis ma dernière visite » (bouton et étiquette, même règle) : libéré ou entré en
    // rédemption depuis l'ouverture précédente de la Veille (heure gardée dans le navigateur) ;
    // à la toute première visite, depuis 24 h.
    dropSince() { return this.dropsSeenBefore || new Date(this.now - 24 * 3600 * 1000).toISOString(); },

    dropNew(d) {
      const at = d.state === "available" ? d.available_since : d.state_since;
      return !!at && at > this.dropSince();
    },

    // Ligne rouge des sources : une panne de réseau (« erreur temporaire », l'appli réessaie à la lecture
    // suivante) ne se règle pas comme un compte refusé ou un fichier illisible (« à configurer »).
    sourcesIssue() {
      const d = this.dropsData;
      if (!d) return null;
      const zone = (d.zones || []).find((z) => z.error);
      const raw = d.error || (zone && zone.error) || (d.fr && d.fr.enabled && d.fr.error) || "";
      if (!raw) return null;
      const where = d.error ? "zones ICANN" : zone ? `.${zone.tld}` : ".fr";
      const plain = plainError(raw);
      if (plain !== raw) return { kind: "temporary", text: `Erreur temporaire (${where}) : ${plain}. Nouvel essai à la prochaine lecture.` };
      return { kind: "config", text: `Pas encore configuré ou refusé (${where}) : ${raw}` };
    },

    sourcesError() {
      const issue = this.sourcesIssue();
      return issue ? issue.text : "";
    },

    sourcesLine() {
      const d = this.dropsData;
      if (!d) return "";
      const error = this.sourcesError();
      if (error) return error;
      const last = (d.zones || []).reduce((m, z) => (z.at && z.at > m ? z.at : m), "");
      const parts = [];
      if (last) parts.push(`zones lues ${this.relative(last)}`);
      if (d.fr && d.fr.enabled && d.fr.at) parts.push(`.fr importé ${this.relative(d.fr.at)}`);
      if (d.gandi && d.gandi.finished_at) parts.push(`prix ${this.relative(d.gandi.finished_at)}`);
      return parts.length ? "Lectures à jour : " + parts.join(", ") + "." : "Lectures à jour.";
    },

    // Avis IA en valeur triable : revente rapide haute, puis note de revente.
    aiValue(d) { return d.ai ? d.ai.quick_high * 1000 + d.ai.resale : -1; },

    // Listes du panneau Avancé : mêmes libellés que les puces de type quand c'est la même liste.
    kindGroup(kinds, shape) {
      return Object.fromEntries(Object.entries(kinds || {}).filter(([k]) => SHAPE_KINDS.includes(k) === shape)
        .map(([k, label]) => [k, this.kindLabel(kinds, k, label)]));
    },

    kindLabel(kinds, k, fallback) {
      const type = EX_TYPES.find((t) => t.k === k);
      return type ? type.label : fallback || (kinds && kinds[k]) || KIND_LABELS[k] || k;
    },

    dropActive() {
      if (!this.dropsData) return [];
      const f = this.df, out = [];
      if (f.q.trim()) out.push({ key: "q", label: `Nom : ${f.q.trim()}` });
      for (const k of this.dropKinds) out.push({ key: "kind:" + k, label: this.dropsData.kinds[k] || k });
      if (f.nat) out.push({ key: "nat", label: this.dropsData.natures[f.nat] });
      if (f.lang) out.push({ key: "lang", label: f.lang === "fr" ? "Français" : "Anglais" });
      if (f.len) out.push({ key: "len", label: this.exLengths.find((o) => o.v === f.len)?.label + " car." });
      if (f.rank) out.push({ key: "rank", label: this.exRanks.find((o) => o.v === f.rank)?.label });
      if (this.dropMin) out.push({ key: "min", label: "Score ≥ " + this.dropMin });
      if (f.junk) out.push({ key: "junk", label: "Suites et mélanges inclus" });
      if (f.ai.length) out.push({ key: "ai", label: "IA : " + f.ai.map((v) => AI_VERDICTS[v]).join(", ") });
      if (f.quick) out.push({ key: "quick", label: `Revente rapide ≥ ${f.quick} €` });
      if (f.gprice !== "" && f.gprice !== null && f.gprice !== undefined) out.push({ key: "gprice", label: `Achat ≤ ${this.fmt(Number(f.gprice))} €` });
      if (f.period) out.push({ key: "period", label: `${this.dropTab === "coming" ? "Libération sous" : "Libérés depuis"} ${this.periodLong(f.period)}` });
      // Réglages simples (en-tête sur ordinateur, panneau Avancé sur téléphone) : ils cachent aussi des noms, on les voit.
      if (this.dropTld.length) out.push({ key: "tld", label: "Extension : " + this.dropTld.map((t) => "." + t).join(", ") });
      if (this.dropNewOnly) out.push({ key: "new", label: "Nouveaux seulement" });
      if (this.dropLevel) out.push({ key: "level", label: "Noms faibles masqués" });
      if (f.nopremium) out.push({ key: "nopremium", label: "Premiums masqués" });
      return out;
    },

    // Noms de l'onglet que les filtres cachent : « 12 masqués par les filtres ».
    dropHiddenCount() {
      if (!this.dropsData || this.dropFocus) return 0;
      const all = this.dropsMemo("all:" + this.dropTab, () => this.dropsData.items.filter((d) => this.dropInTab(d, this.dropTab)).length);
      return Math.max(0, all - this.dropTabCount(this.dropTab));
    },

    dropClear(key) {
      if (key.startsWith("kind:")) { this.dropKinds = this.dropKinds.filter((k) => k !== key.slice(5)); return; }
      if (key === "tld") { this.dropTld = []; return; }
      if (key === "new") { this.dropNewOnly = false; return; }
      if (key === "level") { this.dropLevel = ""; return; }
      this.df[key] = Array.isArray(DROP_FILTER_DEFAULTS[key]) ? [] : DROP_FILTER_DEFAULTS[key];
    },

    dropReset() {
      // « Masquer les premiums » est un réglage simple (comme les noms faibles) : il reste.
      this.df = { ...DROP_FILTER_DEFAULTS, ai: [], nopremium: this.df.nopremium };
      this.dropKinds = [];
    },

    dropSave() {
      try {
        localStorage.setItem("drops.filters", JSON.stringify({
          v: 2, df: this.df, tld: this.dropTld, kinds: this.dropKinds, level: this.dropLevel, sort: this.dropSortMode,
        }));
      } catch (_) {}
    },

    dropRestore() {
      try {
        const saved = JSON.parse(localStorage.getItem("drops.filters") || "null");
        if (!saved) return;
        this.df = { ...DROP_FILTER_DEFAULTS, ...saved.df, ai: Array.isArray(saved.df?.ai) ? saved.df.ai : [] };
        this.df.min = Number(this.df.min) || 0;  // anciens filtres : null voulait dire « pas choisi »
        // Ancien réglage : une seule extension, en texte.
        this.dropTld = Array.isArray(saved.tld) ? saved.tld : saved.tld ? [saved.tld] : [];
        this.dropKinds = Array.isArray(saved.kinds) ? [...new Set(saved.kinds.map((k) => CAT_ALIASES[k] || k))] : [];
        if (typeof saved.level === "string") this.dropLevel = saved.level;
        if (DROP_SORTS.includes(saved.sort)) this.dropSortMode = saved.sort;
        if (!PERIODS.some((p) => p.k === this.df.period)) this.df.period = "";
      } catch (_) {}
    },

    get dropTlds() {
      return this.dropsData ? this.dropsMemo("tlds", () => [...new Set(this.dropsData.items.map((d) => d.tld))].sort()) : [];
    },

    // Comme dans l'Explorer : depuis « Toutes », une puce choisit cette extension seule ; ensuite chaque puce
    // s'ajoute ou se retire ; toutes cochées (ou plus aucune), retour à « Toutes ».
    toggleDropTld(t) {
      const on = this.dropTld.includes(t) ? this.dropTld.filter((x) => x !== t) : [...this.dropTld, t];
      this.dropTld = on.length >= this.dropTlds.length ? [] : this.dropTlds.filter((x) => on.includes(x));
      this.dropShow = 100;
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
        // Sondage de l'état seul (items=0) ; la liste complète est relue une fois à la fin.
        const poll = setInterval(async () => {
          await this.loadDrops(true);
          if (!this.dropsData || !this.dropsData.fr.running) { clearInterval(poll); await this.loadDrops(); }
        }, 10000);
      } catch (e) {
        this.sayError(e, "", () => this.importFr());
      }
    },

    async saveFr() {
      try {
        const res = await this.api("PUT", "/api/settings/fr", { enabled: this.cfg.fr.enabled, url: this.cfg.fr.url || "", rate: this.cfg.fr.rate || null, dns: this.cfg.fr.dns });
        this.cfg.fr = res;
        this.refreshMeta();
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
        // Sondage de l'état seul (items=0) ; la liste complète est relue une fois à la fin.
        const poll = setInterval(async () => {
          await this.loadDrops(true);
          if (!this.dropsData || !this.dropsData.running) { clearInterval(poll); await this.loadDrops(); }
        }, 10000);
      } catch (e) {
        this.sayError(e, "", () => this.scanDrops());
      }
    },

    // Écart de zone refusé : la prochaine lecture de cette extension passe quand même, une fois.
    async acceptGap(tld) {
      try {
        const res = await this.api("POST", "/api/settings/czds/accept-gap", { tld });
        Object.assign(this.dropsData, res);
        this.say(`.${tld} : l'écart sera accepté à la prochaine lecture.`);
      } catch (e) {
        this.sayError(e, `.${tld}`);
      }
    },

    gapLine(tld) {
      const g = this.dropsData && this.dropsData.gap_refused && this.dropsData.gap_refused[tld];
      if (!g) return "";
      return `Lecture refusée ${this.relative(g.at)} : ${this.fmt(g.expected)} noms attendus, ${this.fmt(g.found)} lus (zone tronquée ?).`;
    },

    gapAccepted(tld) { return !!(this.dropsData && (this.dropsData.accept_gap || []).includes(tld)); },

    async saveCzds() {
      this.savingCzds = true;
      try {
        const res = await this.api("PUT", "/api/settings/czds", { username: this.czdsUser, password: this.czdsPass });
        this.cfg.czds = res;
        this.czdsPass = "";
        this.refreshMeta();
        this.say(res.notice || `Compte ICANN accepté : ${res.approved.length} zone(s) autorisée(s).`);
      } catch (e) {
        this.say(e.message, "error");
      }
      this.savingCzds = false;
    },

    async deleteCzds() {
      if (!confirm("Retirer le compte ICANN ? Les lectures de zones s'arrêtent.")) return;
      try {
        await this.api("DELETE", "/api/settings/czds");
        await this.loadSettings();
      } catch (e) {
        this.say(e.message, "error");
      }
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

    // Cercle de chargement : une action passe 2 s (ou dure d'habitude 3 s et plus), il s'affiche avec le temps restant estimé d'après la durée
    // habituelle de la même requête (mémorisée par le navigateur), puis « Encore un instant » une fois l'estimation
    // dépassée. Renvoie la fonction à appeler à la fin de la requête.
    track(method, url) {
      if (Date.now() - (this._lastInput || 0) > 1500) return () => {};
      const key = method + " " + url.split("?")[0].replace(/\/[^/]*[\d.%][^/]*(?=\/|$)/g, "/:x");
      if (!this._durations) {
        try { this._durations = JSON.parse(localStorage.getItem("dw-durations") || "{}"); } catch (_) { this._durations = {}; }
      }
      const job = { key, start: Date.now(), est: this._durations[key] || null };
      this._jobs = this._jobs || new Set();
      this._jobs.add(job);
      if (!this._loaderTimer) this._loaderTimer = setInterval(() => this.loaderTick(), 200);
      return () => {
        const took = Date.now() - job.start;
        // Moyenne glissante : une lenteur passagère ne fausse pas durablement l'estimation.
        this._durations[key] = job.est ? Math.round(job.est * 0.6 + took * 0.4) : took;
        try { localStorage.setItem("dw-durations", JSON.stringify(this._durations)); } catch (_) {}
        this._jobs.delete(job);
        this.loaderTick();
      };
    },

    loaderTick() {
      const now = Date.now();
      let show = null;
      for (const j of this._jobs) {
        const spent = now - j.start;
        // Habituellement long (3 s et plus) : le cercle part presque tout de suite, le compte à rebours se lit en
        // entier. Durée inconnue ou d'habitude courte : seulement passé 2 s.
        if (spent < (j.est >= 3000 ? 400 : 2000)) continue;
        const left = j.est ? Math.max(0, (j.est - spent) / 1000) : null;
        const frac = j.est ? Math.min(1, spent / j.est) : 0;
        if (!show || (left ?? Infinity) > (show.left ?? Infinity)) show = { left, frac, spent };
      }
      if (show) {
        if (!this.loader) this._loaderShownAt = now;
        this._loaderIdle = null;
        this.loader = show;
      } else if (this.loader) {
        // Plus rien en cours : le cercle reste au moins 0,8 s, et 0,3 s de plus si une requête enchaîne
        // (sinon il s'éteint et se rallume entre deux appels : il scintille).
        this._loaderIdle = this._loaderIdle || now;
        if (now - this._loaderShownAt >= 800 && now - this._loaderIdle >= 300) {
          this.loader = null;
          this._loaderIdle = null;
        }
      }
      if (!this._jobs.size && !this.loader) { clearInterval(this._loaderTimer); this._loaderTimer = null; }
    },

    loaderText() {
      const l = this.loader;
      if (!l) return "";
      if (l.left === null) return `Chargement… ${Math.floor(l.spent / 1000)} s`;
      return l.left >= 1 ? `Encore ${Math.ceil(l.left)} s` : "Encore un instant";
    },

    async api(method, url, body) {
      let res;
      const done = this.track(method, url);
      try {
        res = await fetch(url, {
          method,
          headers: body ? { "Content-Type": "application/json" } : {},
          body: body ? JSON.stringify(body) : undefined,
          credentials: "same-origin",
        });
      } catch (_) {
        // Navigateur hors ligne ou NAS injoignable : pas de statut, l'action est rejouable.
        throw Object.assign(new Error("Réseau injoignable"), { status: 0 });
      } finally {
        done();
      }
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

    // Lecture avec empreinte (ETag) : si la réponse n'a pas changé, le serveur répond 304 et l'on garde l'objet
    // déjà affiché, le même : l'écran ne se redessine pas. Renvoie [données, changées ou non].
    async apiFresh(url) {
      this._fresh = this._fresh || {};
      const known = this._fresh[url];
      let res;
      const done = this.track("GET", url);
      try {
        res = await fetch(url, { headers: known ? { "If-None-Match": known.tag } : {}, credentials: "same-origin", cache: "no-store" });
      } catch (_) {
        throw Object.assign(new Error("Réseau injoignable"), { status: 0 });
      } finally {
        done();
      }
      if (res.status === 304 && known) return [known.data, false];
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
      const data = await res.json();
      const tag = res.headers.get("ETag");
      if (tag) this._fresh[url] = { tag, data };
      return [data, true];
    },

    async load(quiet = false) {
      try {
        const [[domains, fresh], meta] = await Promise.all([this.apiFresh("/api/domains"), this.api("GET", "/api/meta")]);
        // Rafraîchissement régulier : la liste n'est remplacée (et redessinée) que si elle a changé.
        if (fresh || !this.loaded) this.domains = domains;
        this.meta = meta;
        this.bulkRunning = meta.bulk_running;
        this.setup = meta.steps || this.setup;
        this.loadError = null;
        this.now = Date.now();
        if (this.fiche && this.fiche.domain_id) this.loadHistory(this.fiche.domain_id);
      } catch (e) {
        if (!quiet || !this.loaded) this.loadError = e.message;
      }
      this.loaded = true;
    },

    // action : bouton dans le message ({ label, run }), par exemple « Annuler » ou « Réessayer ».
    // detail : texte brut sous le message (« Détail : … »), aussi au survol.
    say(text, kind = "info", action = null, detail = "") {
      this.notice = { text, kind, action, detail };
      clearTimeout(this._noticeTimer);
      // Une erreur reste un peu plus longtemps, un message avec bouton aussi ; la croix ferme avant.
      this._noticeTimer = setTimeout(() => (this.notice = null), kind === "error" || action ? 8000 : 6000);
    },

    noticeAct() {
      const action = this.notice && this.notice.action;
      this.notice = null;
      if (action) action.run();
    },

    // Erreur en français simple (réseau, registre muet), avec « Réessayer » quand l'action est rejouable.
    sayError(e, prefix = "", retry = null) {
      const text = plainError(e && e.message);
      this.say(prefix ? `${prefix} : ${text}` : text, "error", retry && retryable(e) ? { label: "Réessayer", run: retry } : null,
        errorDetail(e && e.message));
    },

    // quiet : pas de message (action groupée). Renvoie le domaine ajouté, ou null.
    async add(quiet = false) {
      const name = this.newName.trim();
      if (!name) return null;
      this.adding = true;
      let added = null;
      try {
        const d = await this.api("POST", "/api/domains", { name });
        this.domains.push(d);
        this.newName = "";
        added = d;
        if (!quiet) {
          this.say(d.check_error ? `${d.name} ajouté. Premier check en échec : ${plainError(d.check_error)}.` : `${d.name} ajouté : ${this.stateLabel(d)}.`,
            d.check_error ? "error" : "info", null, errorDetail(d.check_error));
        }
      } catch (e) {
        if (!quiet) this.sayError(e, "", () => { this.newName = name; return this.add(); });
      }
      this.adding = false;
      // Le champ d'ajout n'est visible que sur l'écran Domaines : ailleurs, le focus ouvrirait le clavier derrière la feuille.
      if (!this.paged && !this.fiche) this.$nextTick(() => this.$refs.input.focus());
      return added;
    },

    replace(d) {
      const i = this.domains.findIndex((x) => x.id === d.id);
      if (i >= 0) this.domains[i] = d;
    },

    // announce : message aussi quand tout va bien (bouton Vérifier de la fiche).
    async check(d, announce = false) {
      this.busy[d.id] = true;
      try {
        const updated = await this.api("POST", `/api/domains/${d.id}/check`);
        this.replace(updated);
        this.now = Date.now();
        if (updated.check_error) this.sayError({ message: updated.check_error }, d.name, () => this.check(d, announce));
        else if (announce) this.say(`${d.name} : ${this.stateLabel(updated)}.`);
        if (this.fiche && this.fiche.domain_id === d.id) this.loadHistory(d.id);
      } catch (e) {
        this.sayError(e, d.name, () => this.check(d));
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

    // Renvoie true si le nom a quitté la surveillance.
    async remove(d) {
      if (!confirm(`Supprimer ${d.name} de la surveillance ? L'historique sera effacé.`)) return false;
      this.busy[d.id] = true;
      let done = false;
      try {
        await this.api("DELETE", `/api/domains/${d.id}`);
        this.domains = this.domains.filter((x) => x.id !== d.id);
        this.say(`${d.name} retiré de la surveillance.`);
        done = true;
      } catch (e) {
        this.say(`${d.name} : ${e.message}`, "error");
      }
      this.busy[d.id] = false;
      return done;
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
        this.loadAiOutcomes();
        this.loadTokens();
        this.refreshMeta();
      } catch (e) {
        this.sayError(e, "Paramètres indisponibles", () => this.loadSettings());
      }
    },

    // Carte Alertes : une case par type, seuil des pépites, heure du message du jour, jour du résumé de la semaine.
    async saveAlerts() {
      this.savingAlerts = true;
      try {
        const a = this.cfg.alerts;
        this.cfg.alerts = await this.api("PUT", "/api/settings/alerts", {
          enabled: Object.fromEntries(a.types.map((t) => [t.key, !!t.enabled])),
          pepite_min: Number(a.pepite_min) || 0, daily_hour: Number(a.daily_hour) || 0,
          weekly_day: Number(a.weekly_day) || 0, morning_min: Number(a.morning_min) || 0,
        });
        const on = this.cfg.alerts.types.filter((t) => t.enabled).length;
        this.say(`Alertes enregistrées : ${on} type${on > 1 ? "s" : ""} sur ${this.cfg.alerts.types.length}, pépites dès une note de revente de ${this.cfg.alerts.pepite_min}.`);
      } catch (e) {
        this.sayError(e, "Alertes");
      }
      this.savingAlerts = false;
    },

    // Fiabilité des verdicts : part des noms repris, par verdict et par tranche de note de revente.
    async loadAiOutcomes() {
      try { this.aiOutcomes = await this.api("GET", "/api/ai/outcomes"); } catch (_) {}
    },

    outcomeRows() {
      const o = this.aiOutcomes;
      if (!o || !o.total) return [];
      const rows = [];
      for (const v of Object.values(AI_VERDICTS)) if (o.by_verdict[v]) rows.push({ key: "v:" + v, label: v, ...o.by_verdict[v] });
      for (const [band, cell] of Object.entries(o.by_resale)) if (cell.count) rows.push({ key: "b:" + band, label: `Revente ${band.replace("-", " à ")}`, ...cell });
      return rows;
    },

    share(cell) { return cell.share === null || cell.share === undefined ? "" : `${Math.round(cell.share * 100)} %`; },

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
      // Sondage léger : /api/settings/status, pas tous les réglages (ni un appel à Telegram à chaque tour).
      this._pairTimer = setInterval(async () => {
        const st = await this.settingsStatus();
        if (!st || !this.cfg) return;
        Object.assign(this.cfg.telegram, st.telegram);
        if (!st.telegram.pairing) {
          clearInterval(this._pairTimer);
          this.pairing = null;
          if (st.telegram.chat_set) { this.say("Compte Telegram associé."); this.refreshMeta(); }
        }
      }, 3000);
    },

    async removeTelegram() {
      if (!confirm("Retirer le bot Telegram ? Les alertes ne seront plus envoyées.")) return;
      try {
        await this.api("DELETE", "/api/settings/telegram");
        this.pairing = null;
        await this.loadSettings();
        this.say("Bot retiré.");
      } catch (e) {
        this.say(e.message, "error");
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
      try {
        await this.api("DELETE", "/api/settings/logo");
        window.location.reload();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async regenerateCalendar() {
      if (this.cfg.calendar_url && !confirm("Changer l'adresse ? L'ancienne cessera de fonctionner.")) return;
      try {
        const res = await this.api("POST", "/api/settings/calendar");
        this.cfg.calendar_url = res.calendar_url;
        this.loadTokens();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async loadTokens() {
      try { this.tokenList = await this.api("GET", "/api/tokens"); } catch (_) {}
    },

    tokenDates(t) {
      const made = t.created_at ? "créé " + this.relative(t.created_at) : "";
      const used = t.last_used_at ? "utilisé " + this.relative(t.last_used_at) : "jamais utilisé";
      return [made, used].filter(Boolean).join(" · ");
    },

    async createToken() {
      try {
        const res = await this.api("POST", "/api/tokens", { kind: this.tokenKind, label: this.tokenLabel });
        await this.loadTokens();
        const made = this.tokenList.items.find((t) => t.id === res.id);
        this.tokenNew = { ...res, label: made ? made.label : "" };
        this.tokenLabel = "";
        if (this.tokenKind === "ai") { this.loadAiStatus(); this.refreshMeta(); }
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async renameToken(t) {
      try {
        await this.api("PUT", `/api/tokens/${t.id}`, { label: this.tokenEditLabel });
        this.tokenEdit = null;
        await this.loadTokens();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    async revokeToken(t) {
      if (!confirm(`Révoquer « ${t.label} » ? Ce qui l'utilise perd l'accès tout de suite.`)) return;
      try {
        await this.api("DELETE", `/api/tokens/${t.id}`);
        if (this.tokenNew && this.tokenNew.id === t.id) this.tokenNew = null;
        if (t.kind === "calendar") this.cfg.calendar_url = null;
        await this.loadTokens();
        if (t.kind === "ai") { this.loadAiStatus(); this.refreshMeta(); }
        this.say(`« ${t.label} » révoqué.`);
      } catch (e) {
        this.say(e.message, "error");
      }
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
        this.say("Essai envoyé sur Telegram.");
      } catch (e) {
        this.say(`Essai non envoyé : ${e.message}`, "error");
      }
      this.sendingTest = false;
    },

    async loadHistory(id) {
      try {
        this.history[id] = await this.api("GET", `/api/domains/${id}/history`);
      } catch (_) {
        this.history[id] = [];
      }
    },

    // Signaux d'abandon : seulement pour un nom enregistré.
    registeredState(d) { return ["active", "expiring", "expired", "renewed", "redemption", "pending_delete"].includes(d.state); },

    // Fiche d'un nom surveillé : « , dans 40 jours » après la date d'expiration (rien si passée ou inconnue).
    daysText(d) {
      const n = this.daysLeft(d);
      return n === null || n < 0 ? "" : n === 0 ? ", aujourd'hui" : `, dans ${n} jour${n > 1 ? "s" : ""}`;
    },

    displayState(d) {
      if (d.check_error) return "error";
      return d.state || "unknown";
    },

    stateLabel(d) {
      if (d.check_error) return LABELS.error;
      // Libre au registre mais refusé à l'achat au prix normal (revu au registre après) : même état que l'Explorer.
      if (GANDI_BLOCKED.includes(this.gandiKind(d))) return d.gandi.label;
      // Disponible d'après le seul WHOIS : à confirmer chez un registrar.
      if (d.state === "available" && d.source === "whois") return "Disponible selon le WHOIS, à confirmer";
      return LABELS[d.state] || "Non vérifié";
    },

    // Classe du badge d'état : celle de l'état, ou celle de l'Explorer pour un nom libre refusé à l'achat.
    stateClass(d) {
      return !d.check_error && GANDI_BLOCKED.includes(this.gandiKind(d)) ? "x-7" : "s-" + this.displayState(d);
    },

    stateTitle(d) {
      if (d.check_error) return `${d.check_error}. Dernier état connu conservé.`;
      if (d.state_since) return `Dans cet état depuis le ${this.exact(d.state_since)}`;
      return "";
    },

    // Compteurs d'état : calculés avec les autres filtres (extension, nom, IA, tag…), pas avec l'état lui-même,
    // pour voir combien chaque état garde de noms dans la sélection.
    get counterBase() {
      return this.domains.filter((d) => this.domMatches(d, true));
    },

    get counters() {
      const counts = {};
      for (const d of this.counterBase) {
        const key = this.displayState(d);
        counts[key] = (counts[key] || 0) + 1;
      }
      return [...URGENCY, "error"].filter((k) => counts[k]).map((k) => ({ key: k, label: LABELS[k], count: counts[k] }));
    },

    openSettings(anchor) { return this.openSettingsAt(`set-${anchor}`); },

    get legend() {
      const iv = (this.meta && this.meta.intervals) || {};
      const days = (this.meta && this.meta.expiring_days) || 30;
      const f = (k) => (iv[k] ? frequency(iv[k]) : "");
      return [
        { key: "available", definition: "Le registre ne connaît pas ce nom : probablement enregistrable maintenant.", frequency: f("available"), action: "Enregistrer le nom chez un registrar." },
        { key: "pending_delete", definition: "Plus aucun renouvellement ni restauration possible. Libération dans quelques jours.", frequency: f("pending_delete"), action: "Préparer l'achat ou poser un backorder." },
        { key: "redemption", definition: "Nom supprimé par le registrar. Le titulaire peut encore le restaurer, avec frais.", frequency: f("redemption"), action: "Repérer un service de backorder. La plupart des noms de valeur sont restaurés ici." },
        { key: "expired", definition: "Date d'expiration dépassée, période de grâce. Le titulaire peut encore renouveler.", frequency: f("expired"), action: "Attendre. Beaucoup de noms sont renouvelés pendant cette période." },
        { key: "renewed", definition: "Renouvelé automatiquement à l'échéance, par le même titulaire. Le registrar peut encore le supprimer pendant la période de grâce ; ensuite il redevient Actif.", frequency: f("renewed"), action: "Rien en général : la plupart des noms renouvelés restent à leur titulaire. Une suppression le ferait passer en rédemption." },
        { key: "expiring", definition: `Enregistré, expiration dans moins de ${days} jours.`, frequency: f("expiring"), action: "Surveiller. Les renouvellements de dernière minute sont fréquents." },
        { key: "active", definition: `Enregistré, expiration dans plus de ${days} jours.`, frequency: f("active"), action: "Rien à faire." },
        { key: "error", definition: "Le dernier check a échoué (réseau, limite de requêtes, serveur). Statut inconnu, dernier état connu conservé.", frequency: "selon le dernier état", action: "Rien : le check est retenté. Vérifier à la main si l'erreur persiste." },
      ].map((x) => ({ ...x, label: LABELS[x.key] }));
    },

    // Colonne « Jours restants » : une échéance passée se lit « expiré il y a 40 j », pas « -40 ».
    daysCell(d) {
      const n = this.daysLeft(d);
      return n === null ? "" : n < 0 ? `expiré il y a ${-n} j` : String(n);
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
        // Meilleur d'abord au premier clic.
        case "score": return -(d.score || 0);
        case "ai": return -this.aiValue(d);
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
        this.tldFilter = saved.tldFilter || "";
        this.aiFilter = Array.isArray(saved.aiFilter) ? saved.aiFilter : [];
        if (saved.sortKey) { this.sortKey = saved.sortKey; this.sortDir = saved.sortDir || 1; }
      } catch (_) {}
    },

    saveFilters() {
      try {
        localStorage.setItem("dw.filters", JSON.stringify({
          query: this.query, filter: this.filter, tagFilter: this.tagFilter,
          abandonedOnly: this.abandonedOnly, sortKey: this.sortKey, sortDir: this.sortDir,
          tldFilter: this.tldFilter, aiFilter: this.aiFilter,
        }));
      } catch (_) {}
    },

    get activeFilters() {
      const list = [];
      if (this.query.trim()) list.push({ key: "query", label: `Nom : ${this.query.trim()}` });
      if (this.filter) list.push({ key: "filter", label: `État : ${LABELS[this.filter] || this.filter}` });
      if (this.tagFilter) list.push({ key: "tagFilter", label: `Tag : ${this.tagFilter}` });
      if (this.abandonedOnly) list.push({ key: "abandonedOnly", label: "Signes d'abandon" });
      if (this.tldFilter) list.push({ key: "tldFilter", label: `Extension : .${this.tldFilter}` });
      if (this.aiFilter.length) list.push({ key: "aiFilter", label: "IA : " + this.aiFilter.map((v) => AI_VERDICTS[v]).join(", ") });
      return list;
    },

    clearFilter(key) {
      if (key === "query") this.query = "";
      else if (key === "abandonedOnly") this.abandonedOnly = false;
      else if (key === "tldFilter") this.tldFilter = "";
      else if (key === "aiFilter") this.aiFilter = [];
      else this[key] = null;
    },

    clearFilters() {
      this.query = "";
      this.filter = null;
      this.tagFilter = null;
      this.abandonedOnly = false;
      this.tldFilter = "";
      this.aiFilter = [];
    },

    get domainTlds() {
      return [...new Set(this.domains.map((d) => d.name.split(".").pop()))].sort();
    },

    toggleAiFilter(k) {
      this.aiFilter = this.aiFilter.includes(k) ? this.aiFilter.filter((x) => x !== k) : [...this.aiFilter, k];
    },

    get allTags() {
      return [...new Set(this.domains.flatMap((d) => d.tags))].sort();
    },

    domMatches(d, anyState = false) {
      const q = this.query.trim().toLowerCase();
      return (anyState || !this.filter || this.displayState(d) === this.filter) &&
        (!this.tagFilter || d.tags.includes(this.tagFilter)) &&
        (!this.abandonedOnly || d.abandoned) &&
        (!this.tldFilter || d.name.endsWith("." + this.tldFilter)) &&
        (!this.aiFilter.length || this.aiFilter.includes(this.aiClass(d.ai))) &&
        (!q || d.name.includes(q) || d.unicode_name.toLowerCase().includes(q));
    },

    get rows() {
      const list = this.domains.filter((d) => this.domMatches(d));
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

    // Sur téléphone, les en-têtes du tableau sont masqués : un menu donne les mêmes tris, un bouton inverse le sens.
    DOM_SORTS: [{ k: "urgency", label: "État, le plus urgent d'abord" }, { k: "name", label: "Nom, de A à Z" },
      { k: "score", label: "Valeur, la meilleure d'abord" }, { k: "days", label: "Jours restants, le moins d'abord" },
      { k: "release", label: "Libération, la plus proche d'abord" }, { k: "next", label: "Prochain check, le plus proche d'abord" }],

    setDomSort(key) {
      this.sortKey = key;
      this.sortDir = 1;
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
