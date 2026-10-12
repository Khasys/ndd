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
      if ("serviceWorker" in navigator && window.isSecureContext) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
    },

    get mobile() { return window.matchMedia("(max-width: 640px)").matches; },

    lockScroll() {
      document.body.classList.toggle("sheet-open", this.mobile && (this.legendOpen || this.settingsOpen));
    },

    showList() {
      this.legendOpen = false;
      this.settingsOpen = false;
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    async openSheet(name) {
      if (name === "legend") {
        this.settingsOpen = false;
        this.legendOpen = !this.legendOpen;
      } else {
        this.legendOpen = false;
        await this.toggleSettings();
      }
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
        throw new Error(detail);
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
      this.settingsOpen = !this.settingsOpen;
      if (this.settingsOpen) await this.loadSettings();
    },

    async loadSettings() {
      try {
        this.cfg = await this.api("GET", "/api/settings");
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
        const { app_name, theme } = this.cfg.appearance;
        this.cfg = await this.api("PUT", "/api/settings/appearance", { app_name, theme });
        this.applyAppearance();
      } catch (e) {
        this.say(e.message, "error");
      }
    },

    applyAppearance() {
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
      if (min < 1) return past ? "à l'instant" : "imminent";
      let text;
      if (min < 60) text = `${min} min`;
      else if (min < 60 * 24) text = `${Math.round(min / 60)} h`;
      else if (min < 60 * 24 * 60) text = `${Math.round(min / 1440)} j`;
      else return this.shortDate(v);
      return past ? `il y a ${text}` : `dans ${text}`;
    },
  }));
});
