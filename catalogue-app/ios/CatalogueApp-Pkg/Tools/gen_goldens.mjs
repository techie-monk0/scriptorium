#!/usr/bin/env node
// T2 — golden generator. Loads the REAL library-core.js (the Tier-2 source of truth) and runs its
// view-models over the shared fixtures, writing goldens.json. The Swift parity test (U2) then asserts
// its own view-models produce the same output for the same input → web stays the source of truth.
//
//   node catalogue-app/ios/CatalogueApp-Pkg/Tools/gen_goldens.mjs
//
// Re-run whenever library-core.js changes; commit the regenerated goldens.json.
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..', '..', '..');   // Tools → CatalogueApp → ios → catalogue-app → repo root
const coreSrc = readFileSync(
  join(repo, 'catalogue-webui', 'src', 'catalogue', 'webui', 'static', 'js', 'library-core.js'), 'utf8');
const goldensDir = join(here, '..', 'Tests', 'CatalogueCoreTests', 'Goldens');
const fx = JSON.parse(readFileSync(join(goldensDir, 'fixtures.json'), 'utf8'));

// library-core.js is a browser IIFE that attaches to `window.LibraryCore`. Inject a `window` shim by
// running the source as a function body with `window` as a parameter (no globals, no eval hacks).
const win = {};
new Function('window', coreSrc)(win);
const LC = win.LibraryCore;
if (!LC) throw new Error('LibraryCore did not attach — shim broke');

const platform = {
  data: {
    search: async (q) => fx.search[q] ?? [],
    browse: async (q) => fx.browse[q] ?? { groups: [] },
    content: async (q) => fx.content[q] ?? { books: [], available: true },
    detail: async (eid) => fx.detail[String(eid)] ?? null,
  },
  nav: { hrefFor: () => null },
  prefs: { get: (k) => (k in fx.prefs ? fx.prefs[k] : null), set: () => {}, remove: () => {} },
  isOffline: () => false,
};

const goldens = {
  fold: Object.fromEntries(fx.fold_inputs.map((s) => [s, LC.fold(s)])),
  nameKey: ['14th Dalai Lama', 'Fourteenth Dalai Lama', 'Dalai Lama XIV', 'XIV', 'Volume 14', 'café'].map((s) => LC.nameKey(s)),
  reflowPageText: fx.reflow_inputs.map((s) => LC.reflowPageText(s)),
  readerChromeVM: fx.chrome_inputs.map((i) => LC.readerChromeVM(i)),
  syncVM: fx.sync_inputs.map((s) => LC.syncVM(s)),
  searchVM: { bodhi: await LC.searchVM(platform, 'bodhi'), empty: await LC.searchVM(platform, '') },
  browseVM: { shanti: await LC.browseVM(platform, 'shanti', null), empty: await LC.browseVM(platform, '') },
  contentVM: { emptiness: await LC.contentVM(platform, 'emptiness'), empty: await LC.contentVM(platform, '') },
  detailVM: { '42': await LC.detailVM(platform, 42), '999': await LC.detailVM(platform, 999) },
  homeVM: LC.homeVM(fx.home.replica, fx.home.recentIds, fx.home.starredIds, {}),
  searchReplica: {
    auth: LC.searchReplica(fx.home.replica, 'Auth A').map((c) => c.eid),
    zen: LC.searchReplica(fx.home.replica, 'zen').map((c) => c.eid),
    none: LC.searchReplica(fx.home.replica, 'zzzz').map((c) => c.eid),
    eid: LC.searchReplica(fx.home.replica, '102').map((c) => c.eid),   // edition-number search
    ordinal: LC.searchReplica(fx.home.replica, 'fourteenth dalai lama').map((c) => c.eid),  // 14th/XIV/Fourteenth all hit
  },
  suggestReplica: { auth: LC.suggestReplica(fx.home.replica, 'Auth A') },
  subjectVM: { buddhism: LC.subjectVM(fx.home.replica, 'Buddhism') },
  subjectSections: LC.subjectSections(LC.subjectVM(fx.home.replica, 'Buddhism')),
  wishlistVM: LC.wishlistVM(fx.wishlist, {}),
  wishlistRequest: fx.wishlistActions.map((a) =>
    LC.wishlistRequest(a.action, { body: a.body, id: a.id, index: a.index, editionId: a.editionId })),
  wishlistAddMessage: fx.wishlistAddResponses.map((r) => LC.wishlistAddMessage(r)),
  starredRequest: fx.starredActions.map((a) => LC.starredRequest(a.action, { eid: a.eid })),
  browseReplica: {
    auth: LC.browseReplica(fx.home.replica, 'auth', null),
    zenSubjects: LC.browseReplica(fx.home.replica, 'zen', 'subjects'),   // subject → /subject/<id> nav
    workAlias: LC.browseReplica(fx.home.replica, 'alttitle', 'works'),   // Work search matches an alias spelling
  },
  settingsVM: LC.settingsVM(platform),
  navVM: LC.navVM(fx.nav.items, fx.nav.activeKey, fx.nav.ctx),
  appSections: LC.APP_SECTIONS,
  coverContract: { bookCoverAspect: LC.BOOK_COVER_ASPECT, seriesCoverStyles: LC.SERIES_COVER_STYLES,
                   seriesCoverDefault: LC.SERIES_COVER_DEFAULT },
  searchFields: LC.SEARCH_FIELDS,
  bookDetailSections: LC.BOOK_DETAIL_SECTIONS,
};

writeFileSync(join(goldensDir, 'goldens.json'), JSON.stringify(goldens, null, 2) + '\n');
console.log('wrote goldens.json');
