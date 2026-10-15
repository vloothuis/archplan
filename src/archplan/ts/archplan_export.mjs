#!/usr/bin/env node
// archplan exporter for TypeScript and JavaScript projects.
//
//   node archplan_export.mjs [-C DIR] [-o FILE] [--root DIR] [--depth N] [--tests]
//                            [--exclude GLOB]... [--exclude-base DIR]
//
// Writes an archmodel/1 file (stdout without -o). -C names the project
// directory, the one with package.json (default: the current directory).
// --root is the directory paths in the model are relative to (default: the
// current directory, which is where archplan runs the export). --depth limits
// how deep components nest under src (default: 3). --tests also counts test
// files in dependencies and types; they are always in the files section.
// --exclude leaves out files and folders that match GLOB, relative to
// --exclude-base (default: the current directory); archplan passes the
// "exclude" list from .archplan/config.json.
//
// The script has no dependencies of its own. It uses the typescript package
// the project installed, so run `npm install` in the project first.
//
// Components are directories under src (or the project root). A directory
// with many files is split into its subdirectories. npm packages are external
// components, one per package. Types are classes, interfaces, type aliases,
// enums, exported functions, and React, Vue and Svelte components. Vue and
// Svelte files are read through their <script> blocks. Signatures list
// functions and methods; literals list string values the code compares,
// switches on, assigns or uses as defaults; enums list string unions.

import fs from "node:fs";
import path from "node:path";
import { builtinModules, createRequire } from "node:module";
import { execFileSync } from "node:child_process";

const SKIP = new Set(["node_modules", "dist", "build", "coverage", ".git", ".archplan", ".next", ".nuxt",
  ".svelte-kit", ".output", ".turbo", ".cache", ".vite", "out", "storybook-static", "playwright-report",
  "test-results"]);
const SRC_EXT = new Set([".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]);
const SFC_EXT = new Set([".vue", ".svelte"]);
const TEST_DIRS = new Set(["__tests__", "__mocks__", "__fixtures__", "test", "tests", "e2e"]);
const TEST_FILE = /\.(test|spec)\.[cm]?[jt]sx?$/;
const SPLIT = 30; // a directory with more files than this is split into its subdirectories
const EVIDENCE = 5;
const MAX_LITERALS = 200; // per file
const BUILTINS = new Set(builtinModules);
const PKG_NAME = /^(@[a-z0-9][\w.-]*\/)?[a-z0-9][\w.-]*$/i;
const MANY = new Set(["Array", "ReadonlyArray", "Set", "ReadonlySet", "Map", "ReadonlyMap", "Record", "Iterable",
  "WeakMap", "WeakSet"]);

function fail(msg, code = 1) {
  process.stderr.write(`archplan export-ts: ${msg}\n`);
  process.exit(code);
}

function usage(code) {
  const text = fs.readFileSync(new URL(import.meta.url), "utf8").split("\n").slice(1, 12)
    .map((l) => l.replace(/^\/\/ ?/, "")).join("\n");
  process.stderr.write(text + "\n");
  process.exit(code);
}

function parseArgs(argv) {
  const o = { dir: ".", out: null, root: null, depth: 3, tests: false, exclude: [], excludeBase: "." };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => (i + 1 < argv.length ? argv[++i] : usage(2));
    if (a === "-C") o.dir = val();
    else if (a === "-o" || a === "--output") o.out = val();
    else if (a === "--root") o.root = val();
    else if (a === "--depth") o.depth = Math.max(1, parseInt(val(), 10) || 3);
    else if (a === "--tests") o.tests = true;
    else if (a === "--exclude") o.exclude.push(val());
    else if (a === "--exclude-base") o.excludeBase = val();
    else if (a === "-h" || a === "--help") usage(0);
    else { process.stderr.write(`unknown argument: ${a}\n`); usage(2); }
  }
  return o;
}

const posix = (p) => p.split(path.sep).join("/");
const rel = (from, to) => posix(path.relative(from, to)) || ".";
const inside = (dir, f) => { const r = path.relative(dir, f); return r && !r.startsWith("..") && !path.isAbsolute(r); };

function loadTs(dir) {
  const req = createRequire(path.join(dir, "package.json"));
  let ts;
  try {
    ts = req("typescript");
  } catch (e) {
    if (e.code !== "MODULE_NOT_FOUND") throw e;
    fail(`cannot find the typescript package from ${dir}. Run npm install there first: ` +
      "archplan uses the project's own typescript.");
  }
  if (typeof ts.createProgram !== "function") {
    fail(`typescript ${ts.version || "?"} has no compiler API (createProgram); archplan needs typescript 4.7 to 6.`);
  }
  return ts;
}

// ---------- excludes ----------
// The same matching as archplan's exclude.py: patterns are relative to a base
// folder, ** spans folders, and a match on a folder excludes all under it.

// A glob segment as tokens: "*", "?", a literal character, or {neg, body}.
// Matching walks the tokens with one backtrack point for the last *, without
// regular expressions, so a hostile pattern stays cheap.
function segment(pat) {
  const toks = [];
  for (let i = 0; i < pat.length; i++) {
    const c = pat[i];
    if (c === "[") {
      let j = i + 1;
      if (pat[j] === "!") j++;
      if (pat[j] === "]") j++;
      while (j < pat.length && pat[j] !== "]") j++;
      if (j < pat.length) {
        const body = pat.slice(i + 1, j);
        toks.push(body[0] === "!" ? { neg: true, body: body.slice(1) } : { neg: false, body });
        i = j;
        continue;
      }
    }
    if (!(c === "*" && toks[toks.length - 1] === "*")) toks.push(c);
  }
  return { test: (name) => wildcard(name, toks) };
}

function inClass(ch, body) {
  for (let i = 0; i < body.length;) {
    if (i + 2 < body.length && body[i + 1] === "-") {
      if (body[i] <= ch && ch <= body[i + 2]) return true;
      i += 3;
    } else {
      if (ch === body[i]) return true;
      i++;
    }
  }
  return false;
}

function wildcard(name, toks) {
  let i = 0, j = 0, starJ = -1, starI = 0;
  while (i < name.length) {
    const t = j < toks.length ? toks[j] : undefined;
    if (t === "*") { starJ = j; starI = i; j++; continue; }
    if (t !== undefined && (t === "?" || (typeof t === "object" && inClass(name[i], t.body) !== t.neg) || t === name[i])) {
      i++; j++; continue;
    }
    if (starJ < 0) return false;
    i = ++starI; j = starJ + 1;
  }
  while (j < toks.length && toks[j] === "*") j++;
  return j === toks.length;
}

function compileGlob(pattern) {
  const pats = [];
  for (const x of pattern.split("/")) {
    if (!(x === "**" && pats.length && pats[pats.length - 1] === "**")) pats.push(x);
  }
  return pats.map((x) => (x === "**" ? x : segment(x)));
}

function globMatch(parts, pats) {
  const n = parts.length, m = pats.length;
  let nxt = new Array(m + 1).fill(true);
  for (let j = m - 1; j >= 0; j--) nxt[j] = pats[j] === "**" && nxt[j + 1];
  for (let i = n - 1; i >= 0; i--) {
    const ok = new Array(m + 1).fill(false);
    for (let j = m - 1; j >= 0; j--) {
      ok[j] = pats[j] === "**" ? ok[j + 1] || nxt[j] : nxt[j + 1] && pats[j].test(parts[i]);
    }
    nxt = ok;
  }
  return nxt[0];
}

function makeExclude(patterns, base) {
  const globs = patterns.map((p) => p.trim().replace(/\\/g, "/").replace(/^(\.\/)+/, "").replace(/^\/+|\/+$/g, ""))
    .filter(Boolean).map(compileGlob);
  if (!globs.length) return () => false;
  base = path.resolve(base);
  return (f) => {
    const r = path.relative(base, path.resolve(f));
    if (!r || r.startsWith("..") || path.isAbsolute(r)) return false;
    const parts = r.split(path.sep);
    for (let i = 1; i <= parts.length; i++) {
      if (globs.some((g) => globMatch(parts.slice(0, i), g))) return true;
    }
    return false;
  };
}

function readJson(f) {
  try { return JSON.parse(fs.readFileSync(f, "utf8")); } catch { return null; }
}

function walk(dir, excluded, out = []) {
  let ents;
  try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of ents) {
    if (e.name.startsWith(".") || SKIP.has(e.name)) continue;
    const p = path.join(dir, e.name);
    if (excluded(p)) continue;
    if (e.isDirectory()) walk(p, excluded, out);
    else if (e.isFile() && SRC_EXT.has(path.extname(e.name))) out.push(p);
  }
  return out;
}

// ---------- Vue and Svelte files ----------

const sfcCache = new Map();
const SFC_MAX = 4 << 20; // bytes; a larger .vue or .svelte file is skipped

// The <script> blocks of a single-file component. Everything else becomes
// spaces, so offsets and line numbers stay those of the real file.
function sfc(file) {
  if (sfcCache.has(file)) return sfcCache.get(file);
  let text;
  try {
    if (fs.lstatSync(file).size > SFC_MAX) throw new Error("too large");
    text = fs.readFileSync(file, "utf8");
  } catch { sfcCache.set(file, null); return null; }
  const chars = text.replace(/[^\n]/g, " ").split("");
  let tsx = false;
  // indexOf instead of a lazy regex: a file with many unclosed <script> tags
  // would make the regex scan to the end from each one.
  const lower = text.toLowerCase();
  let pos = 0;
  for (;;) {
    const open = lower.indexOf("<script", pos);
    if (open < 0) break;
    if (/\w/.test(text[open + 7] || "")) { pos = open + 7; continue; }
    const gt = text.indexOf(">", open);
    if (gt < 0) break;
    const close = lower.indexOf("</script>", gt);
    if (close < 0) break;
    for (let i = gt + 1; i < close; i++) chars[i] = text[i];
    if (/\blang\s*=\s*["']?(tsx|jsx)/.test(text.slice(open + 7, gt))) tsx = true;
    pos = close + 9;
  }
  const v = { text: chars.join(""), tsx };
  sfcCache.set(file, v);
  return v;
}

// ---------- the program ----------

function loadConfig(ts, dir) {
  const extra = [".vue", ".svelte"].map((extension) => ({ extension, isMixedContent: true,
    scriptKind: ts.ScriptKind.Deferred }));
  const host = { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} };
  for (const name of ["tsconfig.json", "jsconfig.json"]) {
    const f = path.join(dir, name);
    if (!fs.existsSync(f)) continue;
    const pc = ts.getParsedCommandLineOfConfigFile(f, {}, host, undefined, undefined, extra);
    if (!pc) continue;
    let files = [...pc.fileNames];
    let options = pc.options;
    let best = files.length;
    // A solution-style config lists its projects as references.
    for (const ref of pc.projectReferences || []) {
      const rf = ts.resolveProjectReferencePath ? ts.resolveProjectReferencePath(ref) : ref.path;
      const rc = fs.existsSync(rf) ? ts.getParsedCommandLineOfConfigFile(rf, {}, host, undefined, undefined, extra) : null;
      if (!rc) continue;
      files = files.concat(rc.fileNames);
      if (rc.fileNames.length > best) {
        best = rc.fileNames.length;
        if (!pc.fileNames.length) options = rc.options;
      }
    }
    if (files.length) return { files, options, config: name };
  }
  return null;
}

function defaultOptions(ts) {
  return {
    allowJs: true, checkJs: false, jsx: ts.JsxEmit.Preserve, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler ?? ts.ModuleResolutionKind.NodeJs,
    target: ts.ScriptTarget.ES2022, esModuleInterop: true, allowSyntheticDefaultImports: true,
  };
}

const VIRTUAL = /\.(vue|svelte)\.(ts|tsx)$/;

function makeHost(ts, options) {
  const host = ts.createCompilerHost(options, true);
  const exists = host.fileExists.bind(host);
  const read = host.readFile.bind(host);
  const get = host.getSourceFile.bind(host);
  const virtual = (name) => {
    if (!VIRTUAL.test(name) || exists(name)) return null;
    const real = name.replace(/\.tsx?$/, "");
    if (!exists(real)) return null;
    const v = sfc(real);
    return v && v.tsx === name.endsWith(".tsx") ? v : null;
  };
  host.fileExists = (name) => exists(name) || !!virtual(name);
  host.readFile = (name) => { const v = virtual(name); return v ? v.text : read(name); };
  host.getSourceFile = (name, lang, onError, create) => {
    const v = virtual(name);
    if (v) return ts.createSourceFile(name, v.text, lang, true, v.tsx ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    return get(name, lang, onError, create);
  };
  return host;
}

const programName = (f) => (SFC_EXT.has(path.extname(f)) ? f + (sfc(f)?.tsx ? ".tsx" : ".ts") : f);
const realName = (f) => (VIRTUAL.test(f) && !fs.existsSync(f) ? f.replace(/\.tsx?$/, "") : f);

// ---------- components ----------

function testDirCut(dirs) {
  const i = dirs.findIndex((d) => TEST_DIRS.has(d));
  return i < 0 ? dirs : dirs.slice(0, i);
}

function components(files, projectDir, base, root, maxDepth) {
  // scope: "b" for files under base (src), "p" for other files of the project.
  const where = (f) => {
    const inBase = base === projectDir || inside(base, f);
    const r = path.relative(inBase ? base : projectDir, f).split(path.sep);
    return { scope: inBase ? "b" : "p", dirs: testDirCut(r.slice(0, -1)) };
  };
  const total = new Map();
  const kids = new Map();
  for (const f of files) {
    const { scope, dirs } = where(f);
    for (let i = 0; i <= dirs.length; i++) {
      const k = scope + ":" + dirs.slice(0, i).join("/");
      total.set(k, (total.get(k) || 0) + 1);
      if (i < dirs.length) {
        if (!kids.has(k)) kids.set(k, new Set());
        kids.get(k).add(dirs[i]);
      }
    }
  }
  const split = (k, depth) => depth < maxDepth && (total.get(k) || 0) > SPLIT && (kids.get(k)?.size || 0) >= 2;
  const baseName = base === projectDir ? path.basename(projectDir) : path.basename(base);
  const projName = path.basename(projectDir);
  const compOf = new Map();
  const info = new Map(); // id -> {dir (abs), files: [], top}
  for (const f of files) {
    const { scope, dirs } = where(f);
    // Files right under base (or the project root) form one component; below
    // that, the first directory, split further while it is large.
    let n = dirs.length ? 1 : 0;
    while (n > 0 && n < dirs.length && split(scope + ":" + dirs.slice(0, n).join("/"), n)) n++;
    const parts = dirs.slice(0, n);
    let id = parts.length ? parts.join("/") : (scope === "b" ? baseName : projName);
    if (scope === "p" && parts.length) id = projName + "/" + id;
    const dir = path.join(scope === "b" ? base : projectDir, ...parts);
    if (!info.has(id)) info.set(id, { dir, files: [], top: parts.length === 0 });
    info.get(id).files.push(f);
    compOf.set(f, id);
  }
  const comps = {};
  for (const [id, c] of [...info].sort((a, b) => a[0].localeCompare(b[0]))) {
    const p = rel(root, c.dir);
    comps[id] = {
      name: c.top ? id : path.basename(c.dir), path: p,
      // A top component holds only the files directly in its folder; folders
      // below it are other components.
      match: c.top ? c.files.map((f) => rel(root, f)).sort() : [p + "/**"],
      kind: "internal",
    };
  }
  return { compOf, comps };
}

// ---------- main ----------

function git(root, args) {
  try {
    return execFileSync("git", ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", ...args],
      { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 30000 }).trim();
  } catch { return null; }
}

function main() {
  const t0 = Date.now();
  const opt = parseArgs(process.argv.slice(2));
  const projectDir = path.resolve(opt.dir);
  const root = path.resolve(opt.root || ".");
  if (!fs.existsSync(projectDir)) fail(`no directory ${projectDir}`);
  const ts = loadTs(projectDir);
  const pkg = readJson(path.join(projectDir, "package.json")) || {};

  const cfg = loadConfig(ts, projectDir);
  const excluded = makeExclude(opt.exclude, opt.excludeBase);
  let files = (cfg ? cfg.files.map((f) => path.resolve(f)) : walk(projectDir, excluded)).filter((f) => {
    if (!inside(projectDir, f) || !SRC_EXT.has(path.extname(f)) || excluded(f)) return false;
    return !path.relative(projectDir, f).split(path.sep).slice(0, -1).some((d) => SKIP.has(d) || d.startsWith("."));
  });
  files = [...new Set(files)].sort();
  if (!files.length) fail(`found no .ts, .tsx, .js, .jsx, .vue or .svelte files in ${projectDir}`);
  const options = { ...(cfg ? cfg.options : defaultOptions(ts)), noEmit: true, skipLibCheck: true };
  if (!cfg) options.allowJs = true;
  const srcDir = path.join(projectDir, "src");
  const base = files.some((f) => inside(srcDir, f)) ? srcDir : projectDir;

  const host = makeHost(ts, options);
  const program = ts.createProgram({ rootNames: files.map(programName), options, host });
  const checker = program.getTypeChecker();
  const cache = ts.createModuleResolutionCache(projectDir, (x) => x, options);
  const isTest = (f) => TEST_FILE.test(f) || testDirCut(path.relative(projectDir, f).split(path.sep).slice(0, -1)).length
    !== path.relative(projectDir, f).split(path.sep).length - 1;
  const fileSet = new Set(files);
  const { compOf, comps } = components(files, projectDir, base, root, opt.depth);
  const relOf = (f) => rel(root, f);
  const modOf = (f) => {
    const r = posix(path.relative(inside(base, f) || base === projectDir ? base : projectDir, f));
    return r.replace(/\.(d\.)?[cm]?[jt]sx?$|\.(vue|svelte)$/, "");
  };
  const counted = (f) => opt.tests || !isTest(f);
  const lineOf = (sf, node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  const sources = new Map(); // real file -> SourceFile
  for (const f of files) {
    const sf = program.getSourceFile(programName(f));
    if (sf) sources.set(f, sf);
  }
  const reexports = new Set();
  for (const [f, sf] of sources) {
    if (sf.statements.some((s) => ts.isExportDeclaration(s) && s.moduleSpecifier)) reexports.add(f);
  }

  // ----- imports -----
  const unresolved = new Map();
  const resolve = (spec, f) => {
    // {file} for a project file, {ext} for a package, or null.
    const r = ts.resolveModuleName(spec, programName(f), options, host, cache).resolvedModule;
    if (r) {
      const target = realName(path.resolve(r.resolvedFileName));
      if (fileSet.has(target)) return { file: target };
      if (!r.isExternalLibraryImport && !target.includes(`${path.sep}node_modules${path.sep}`)) {
        if (inside(projectDir, target)) return null; // a project file outside the sources, or a .d.ts shim
      }
    }
    if (spec.startsWith(".") || spec.startsWith("/")) {
      const t = path.resolve(path.dirname(f), spec);
      if (fileSet.has(t)) return { file: t };
      if (!r && SRC_EXT.has(path.extname(spec))) unresolved.set(spec, f);
      return null;
    }
    if (spec.startsWith("node:")) return { ext: "node" };
    const name = spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0];
    if (BUILTINS.has(name)) return { ext: "node" };
    if (!PKG_NAME.test(name)) {
      if (!r && !/\.(css|scss|sass|less|svg|png|jpe?g|gif|webp|json|html|md)(\?|$)/.test(spec)) unresolved.set(spec, f);
      return null;
    }
    return { ext: r?.packageId?.name || name };
  };

  // The files that declare what a named import or re-export brings in, when
  // the module only passes it through (an index.ts barrel).
  const declFiles = (nameNode) => {
    let sym = checker.getSymbolAtLocation(nameNode);
    if (!sym) return [];
    if (sym.flags & ts.SymbolFlags.Alias) {
      try { sym = checker.getAliasedSymbol(sym); } catch { return []; }
    }
    const out = new Set();
    for (const d of sym.declarations || []) {
      const real = realName(path.resolve(d.getSourceFile().fileName));
      if (!fileSet.has(real)) return [];
      out.add(real);
    }
    return [...out];
  };

  const imports = new Map(); // file -> [{file|ext, line, typeOnly}]
  const defaultImports = new Map(); // file -> Map(local name -> imported file)
  for (const [f, sf] of sources) {
    const out = [];
    const defs = new Map();
    const add = (spec, node, typeOnly, names) => {
      const r = resolve(spec, f);
      if (!r) return;
      const line = lineOf(sf, node);
      if (r.file && names && names.length && reexports.has(r.file)) {
        const real = new Set(names.flatMap(declFiles));
        if (real.size) {
          for (const t of real) out.push({ file: t, line, typeOnly });
          return;
        }
      }
      out.push({ ...r, line, typeOnly });
    };
    const visit = (node) => {
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
        const c = node.importClause;
        const named = c?.namedBindings && ts.isNamedImports(c.namedBindings) ? c.namedBindings.elements : [];
        const typeOnly = !!c && (c.isTypeOnly || (!c.name && named.length > 0 && named.every((e) => e.isTypeOnly)));
        const ns = c?.namedBindings && ts.isNamespaceImport(c.namedBindings);
        add(node.moduleSpecifier.text, node, typeOnly, ns || c?.name ? null : named.map((e) => e.name));
        if (c?.name) {
          const r = resolve(node.moduleSpecifier.text, f);
          if (r?.file) defs.set(c.name.text, r.file);
        }
        return;
      }
      if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
        const named = node.exportClause && ts.isNamedExports(node.exportClause) ? node.exportClause.elements : null;
        add(node.moduleSpecifier.text, node, node.isTypeOnly, named ? named.map((e) => e.propertyName || e.name) : null);
        return;
      }
      if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)
        && ts.isStringLiteral(node.moduleReference.expression)) {
        add(node.moduleReference.expression.text, node, node.isTypeOnly, null);
        return;
      }
      if (ts.isCallExpression(node) && node.arguments.length && ts.isStringLiteralLike(node.arguments[0])
        && (node.expression.kind === ts.SyntaxKind.ImportKeyword
          || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
        add(node.arguments[0].text, node, false, null);
      } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)
        && ts.isStringLiteral(node.argument.literal)) {
        add(node.argument.literal.text, node, true, null);
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
    imports.set(f, out);
    defaultImports.set(f, defs);
  }

  // ----- types -----
  const types = {};
  const declId = new Map(); // declaration node -> type id
  const sfcId = new Map(); // Vue or Svelte file -> its type id
  const pending = []; // [id, node, sf, f, kind]
  const isExported = (n) => !!(ts.getCombinedModifierFlags(n) & ts.ModifierFlags.Export)
    || (n.parent && ts.isSourceFile(n.parent) && n.parent.statements.some((s) =>
      ts.isExportAssignment(s) && ts.isIdentifier(s.expression) && n.name && s.expression.text === n.name.text));
  const hasJsx = (node) => {
    let found = false;
    const v = (n) => {
      if (found) return;
      if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) { found = true; return; }
      ts.forEachChild(n, v);
    };
    v(node);
    return found;
  };
  const fnOf = (init) => {
    // The function in `const X = () => ...`, also when wrapped: memo(...), forwardRef(...).
    let e = init;
    while (e && ts.isCallExpression(e) && e.arguments.length) e = e.arguments[e.arguments.length - 1];
    while (e && (ts.isParenthesizedExpression(e) || ts.isAsExpression(e))) e = e.expression;
    return e && (ts.isArrowFunction(e) || ts.isFunctionExpression(e)) ? e : null;
  };
  const idTaken = (id) => (types[id] || pending.some((p) => p[0] === id));
  const uniqueId = (id) => { let x = id, i = 2; while (idTaken(x)) x = `${id}#${i++}`; return x; };

  for (const [f, sf] of sources) {
    if (!counted(f)) continue;
    const mod = modOf(f);
    const isSfc = SFC_EXT.has(path.extname(f));
    const reg = (name, node, kind) => {
      const id = uniqueId(`${mod}.${name}`);
      declId.set(node, id);
      pending.push([id, node, sf, f, kind, name]);
      return id;
    };
    if (isSfc) {
      const name = path.basename(f).replace(/\.(vue|svelte)$/, "");
      const id = uniqueId(`${mod}.${name}`);
      sfcId.set(f, id);
      pending.push([id, sf, sf, f, "module", name]);
    }
    for (const s of sf.statements) {
      if (isSfc && !isExported(s)) continue;
      const fileName = path.basename(f).replace(/\..*$/, "");
      if (ts.isClassDeclaration(s)) reg(s.name ? s.name.text : fileName, s, "class");
      else if (ts.isInterfaceDeclaration(s)) reg(s.name.text, s, "interface");
      else if (ts.isEnumDeclaration(s)) reg(s.name.text, s, "enum");
      else if (ts.isTypeAliasDeclaration(s)) reg(s.name.text, s, ts.isTypeLiteralNode(s.type) ? "record" : "alias");
      else if (ts.isFunctionDeclaration(s) && s.body && isExported(s)) reg(s.name ? s.name.text : fileName, s, "function");
      else if (ts.isVariableStatement(s) && isExported(s)) {
        for (const d of s.declarationList.declarations) {
          if (ts.isIdentifier(d.name) && fnOf(d.initializer)) reg(d.name.text, d, "function");
        }
      } else if (ts.isExportAssignment(s) && !s.isExportEquals && fnOf(s.expression)) {
        reg(fileName, s, "function");
      }
    }
  }

  const typeIdAt = (node, f) => {
    let sym = checker.getSymbolAtLocation(node);
    if (sym) {
      if (sym.flags & ts.SymbolFlags.Alias) {
        try { sym = checker.getAliasedSymbol(sym); } catch { /* unknown */ }
      }
      for (const d of sym.declarations || []) {
        const id = declId.get(d) || (ts.isVariableDeclaration(d) ? declId.get(d) : null);
        if (id) return id;
        if (ts.isSourceFile(d)) {
          const s = sfcId.get(realName(path.resolve(d.fileName)));
          if (s) return s;
        }
      }
    }
    if (ts.isIdentifier(node)) {
      const t = defaultImports.get(f)?.get(node.text);
      if (t && sfcId.has(t)) return sfcId.get(t);
    }
    return null;
  };
  const typeNames = (node, f, out = []) => {
    const v = (n) => {
      let name = null;
      if (ts.isTypeReferenceNode(n)) name = n.typeName;
      else if (ts.isExpressionWithTypeArguments(n)) name = n.expression;
      else if (ts.isTypeQueryNode(n)) name = n.exprName;
      if (name) {
        const id = typeIdAt(ts.isQualifiedName(name) ? name.right : ts.isPropertyAccessExpression(name) ? name.name : name, f);
        if (id && !out.includes(id)) out.push(id);
      }
      ts.forEachChild(n, v);
    };
    if (node) v(node);
    return out;
  };
  const short = (s) => { s = s.replace(/\s+/g, " ").trim(); return s.length > 160 ? s.slice(0, 157) + "..." : s; };
  const unwrap = (t) => {
    // T | undefined | null -> T
    if (t && ts.isUnionTypeNode(t)) {
      const rest = t.types.filter((x) => !(x.kind === ts.SyntaxKind.UndefinedKeyword
        || (ts.isLiteralTypeNode(x) && x.literal.kind === ts.SyntaxKind.NullKeyword)));
      if (rest.length === 1) return rest[0];
    }
    return t;
  };
  const isMany = (t) => {
    t = unwrap(t);
    if (!t) return false;
    if (ts.isArrayTypeNode(t)) return true;
    if (ts.isTypeOperatorNode(t)) return isMany(t.type);
    if (ts.isTypeReferenceNode(t)) {
      const n = ts.isIdentifier(t.typeName) ? t.typeName.text : t.typeName.right.text;
      return MANY.has(n);
    }
    return false;
  };
  const field = (name, typeNode, f, fallback) => {
    if (!typeNode) return fallback ? { name, type: short(fallback) } : { name };
    const refs = typeNames(typeNode, f);
    if (refs.length === 1) return { name, ref: refs[0], ...(isMany(typeNode) ? { many: true } : {}) };
    return { name, type: short(typeNode.getText()) };
  };
  const sigOf = (fn) => short(`(${fn.parameters.map((p) => p.getText()).join(", ")})`
    + (fn.type ? ": " + fn.type.getText() : ""));
  const memberName = (m) => (m.name && (ts.isIdentifier(m.name) || ts.isStringLiteral(m.name)
    || ts.isPrivateIdentifier(m.name) || ts.isNumericLiteral(m.name)) ? m.name.text : null);
  const hidden = (m) => (m.name && ts.isPrivateIdentifier(m.name))
    || !!(ts.getCombinedModifierFlags(m) & ts.ModifierFlags.Private);
  const membersOf = (members, f, fields, methods, uses) => {
    for (const m of members) {
      const name = memberName(m);
      if (!name || hidden(m)) continue;
      if (ts.isPropertySignature(m) || ts.isPropertyDeclaration(m)) {
        if (m.type && ts.isFunctionTypeNode(m.type)) {
          methods.push({ name, sig: sigOf(m.type) });
          for (const p of m.type.parameters) typeNames(p.type, f, uses);
          typeNames(m.type.type, f, uses);
        } else if (!fields.some((x) => x.name === name)) {
          fields.push(field(name, m.type, f, m.initializer && !m.type ? m.initializer.getText() : null));
        }
      } else if (ts.isMethodSignature(m) || ts.isMethodDeclaration(m)) {
        if (methods.some((x) => x.name === name)) continue; // overloads: keep the first
        methods.push({ name, sig: sigOf(m) });
        for (const p of m.parameters) typeNames(p.type, f, uses);
        typeNames(m.type, f, uses);
      } else if (ts.isGetAccessorDeclaration(m) && !fields.some((x) => x.name === name)) {
        fields.push(field(name, m.type, f, null));
      }
    }
  };
  const propsFromType = (typeNode, f, fields, at) => {
    // Props of a component: the members of a type literal, else the
    // properties the checker finds for the type.
    typeNode = unwrap(typeNode);
    if (!typeNode) return;
    if (ts.isTypeLiteralNode(typeNode)) {
      membersOf(typeNode.members, f, fields, [], []);
      return;
    }
    let type;
    try { type = checker.getTypeFromTypeNode(typeNode); } catch { return; }
    for (const p of type.getProperties().slice(0, 60)) {
      if (fields.some((x) => x.name === p.name)) continue;
      const d = p.valueDeclaration || (p.declarations || [])[0];
      if (d && (ts.isPropertySignature(d) || ts.isPropertyDeclaration(d)) && d.type) {
        fields.push(field(p.name, d.type, d.getSourceFile() === at ? f : realName(path.resolve(d.getSourceFile().fileName)), null));
      } else {
        let t = "";
        try { t = checker.typeToString(checker.getTypeOfSymbolAtLocation(p, typeNode)); } catch { /* none */ }
        fields.push(t ? { name: p.name, type: short(t) } : { name: p.name });
      }
    }
  };
  const docOf = (node) => {
    const holder = ts.isVariableDeclaration(node) ? node.parent?.parent : node;
    const jd = holder?.jsDoc;
    if (!jd || !jd.length) return null;
    const c = jd[jd.length - 1].comment;
    const text = typeof c === "string" ? c : c ? ts.getTextOfJSDocComment(c) : "";
    const first = (text || "").trim().split("\n")[0].trim();
    return first || null;
  };
  const jsxTags = (node, f, out) => {
    const v = (n) => {
      if ((ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) && ts.isIdentifier(n.tagName)
        && /^[A-Z]/.test(n.tagName.text)) {
        const id = typeIdAt(n.tagName, f);
        if (id && !out.includes(id)) out.push(id);
      }
      ts.forEachChild(n, v);
    };
    v(node);
  };
  const importedTypes = (sf, f, out) => {
    for (const s of sf.statements) {
      if (!ts.isImportDeclaration(s) || !s.importClause) continue;
      const c = s.importClause;
      const names = [];
      if (c.name) names.push(c.name);
      if (c.namedBindings && ts.isNamedImports(c.namedBindings)) names.push(...c.namedBindings.elements.map((e) => e.name));
      for (const n of names) {
        const id = typeIdAt(n, f);
        if (id && !out.includes(id)) out.push(id);
      }
    }
  };
  const vueProps = (sf, f, fields) => {
    const v = (n) => {
      if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === "defineProps") {
        if (n.typeArguments?.length) propsFromType(n.typeArguments[0], f, fields, sf);
        else if (n.arguments[0] && ts.isObjectLiteralExpression(n.arguments[0])) {
          for (const p of n.arguments[0].properties) {
            const name = memberName(p);
            if (name) fields.push(ts.isPropertyAssignment(p) ? { name, type: short(p.initializer.getText()) } : { name });
          }
        } else if (n.arguments[0] && ts.isArrayLiteralExpression(n.arguments[0])) {
          for (const e of n.arguments[0].elements) if (ts.isStringLiteral(e)) fields.push({ name: e.text });
        }
        return;
      }
      if (ts.isVariableStatement(n) && path.extname(f) === ".svelte"
        && (ts.getCombinedModifierFlags(n) & ts.ModifierFlags.Export)) {
        for (const d of n.declarationList.declarations) {
          if (ts.isIdentifier(d.name)) fields.push(field(d.name.text, d.type, f, d.initializer?.getText()));
        }
      }
      ts.forEachChild(n, v);
    };
    v(sf);
  };

  for (const [id, node, sf, f, kind0, name] of pending) {
    let kind = kind0;
    const fields = [], methods = [], uses = [], implements_ = [];
    let doc = null;
    if (kind === "module") {
      vueProps(sf, f, fields);
      importedTypes(sf, f, uses);
      doc = `${path.extname(f) === ".vue" ? "Vue" : "Svelte"} component`;
    } else if (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) {
      for (const h of node.heritageClauses || []) {
        for (const t of h.types) {
          const tid = typeIdAt(t.expression, f);
          if (tid && !implements_.includes(tid)) implements_.push(tid);
        }
      }
      membersOf(node.members, f, fields, methods, uses);
      if (ts.isClassDeclaration(node)) {
        const ctor = node.members.find(ts.isConstructorDeclaration);
        for (const p of ctor?.parameters || []) {
          const mods = ts.getCombinedModifierFlags(p);
          if (ts.isIdentifier(p.name) && mods & (ts.ModifierFlags.Public | ts.ModifierFlags.Protected
            | ts.ModifierFlags.Readonly) && !(mods & ts.ModifierFlags.Private)) {
            fields.push(field(p.name.text, p.type, f, null));
          }
          typeNames(p.type, f, uses);
        }
        if (node.heritageClauses?.some((h) => h.types.some((t) => /Component$/.test(t.expression.getText())))
          && hasJsx(node)) doc = "React component";
      }
    } else if (ts.isEnumDeclaration(node)) {
      for (const m of node.members) {
        const n = memberName(m);
        if (n) fields.push({ name: n });
      }
    } else if (ts.isTypeAliasDeclaration(node)) {
      if (ts.isTypeLiteralNode(node.type)) membersOf(node.type.members, f, fields, methods, uses);
      else typeNames(node.type, f, uses);
    } else {
      // A function: a component when it returns JSX and is named like one.
      const fn = ts.isFunctionDeclaration(node) ? node : fnOf(ts.isVariableDeclaration(node) ? node.initializer : node.expression);
      const component = /^[A-Z]/.test(name) && hasJsx(fn);
      if (component) {
        doc = "React component";
        const p = fn.parameters[0];
        if (p?.type) propsFromType(p.type, f, fields, sf);
        else if (p && ts.isObjectBindingPattern(p.name)) {
          for (const e of p.name.elements) if (ts.isIdentifier(e.name)) fields.push({ name: e.name.text });
        }
        jsxTags(fn, f, uses);
        importedTypes(sf, f, uses);
      } else {
        methods.push({ name, sig: sigOf(fn) });
      }
      for (const p of fn.parameters) typeNames(p.type, f, uses);
      typeNames(fn.type, f, uses);
    }
    doc = docOf(node) || doc;
    const frefs = new Set(fields.map((x) => x.ref));
    const used = uses.filter((u) => u !== id && !frefs.has(u) && !implements_.includes(u));
    types[id] = {
      name, component: compOf.get(f), kind, file: relOf(f), line: kind === "module" ? 1 : lineOf(sf, node),
      ...(doc ? { doc } : {}), fields, methods, implements: implements_, uses: used,
    };
  }

  // ----- signatures, literals and enums -----
  const signatures = [];
  const literals = [];
  const enums = [];
  const typeText = (t) => (t ? short(t.getText()) : null);
  const checkedType = (node) => {
    try {
      const s = checker.typeToString(checker.getTypeAtLocation(node), undefined, ts.TypeFormatFlags.NoTruncation);
      return s && s !== "any" ? short(s) : null;
    } catch { return null; }
  };
  const checkedReturn = (fn) => {
    try {
      const sig = checker.getSignatureFromDeclaration(fn);
      const s = sig && checker.typeToString(checker.getReturnTypeOfSignature(sig));
      return s && s !== "any" ? short(s) : null;
    } catch { return null; }
  };
  const nameOf = (e) => {
    // The name a literal is compared with or assigned to: status, x.status, x["status"], getStatus().
    while (e && (ts.isParenthesizedExpression(e) || ts.isAsExpression(e) || ts.isNonNullExpression(e))) e = e.expression;
    if (!e) return null;
    if (ts.isIdentifier(e) || ts.isPrivateIdentifier(e)) return e.text;
    if (ts.isPropertyAccessExpression(e)) return e.name.text;
    if (ts.isElementAccessExpression(e) && ts.isStringLiteralLike(e.argumentExpression)) return e.argumentExpression.text;
    if (ts.isCallExpression(e)) return nameOf(e.expression);
    return null;
  };
  const strOf = (e) => (e && ts.isStringLiteralLike(e) ? e.text : null);
  const fnName = (fn) => {
    if (fn.name && (ts.isIdentifier(fn.name) || ts.isStringLiteral(fn.name))) return fn.name.text;
    if (fn.parent && ts.isVariableDeclaration(fn.parent) && ts.isIdentifier(fn.parent.name)) return fn.parent.name.text;
    if (fn.parent && ts.isPropertyAssignment(fn.parent)) return memberName(fn.parent);
    return null;
  };
  const sigEntry = (id, fn, f, sf, name, pub) => {
    signatures.push({
      id, component: compOf.get(f), file: relOf(f), line: lineOf(sf, fn), name,
      params: fn.parameters.map((p) => ({
        name: ts.isIdentifier(p.name) ? p.name.text : short(p.name.getText()),
        type: typeText(p.type) || checkedType(p),
      })),
      returns: typeText(fn.type) || checkedReturn(fn), public: pub,
    });
  };
  for (const [f, sf] of sources) {
    if (!counted(f)) continue;
    const mod = modOf(f);
    const comp = compOf.get(f);
    const isSfc = SFC_EXT.has(path.extname(f));
    // Signatures: top-level functions and arrow-function consts, and class methods.
    for (const s of sf.statements) {
      const pub = !isSfc && isExported(s);
      if (ts.isFunctionDeclaration(s) && s.body) {
        const name = s.name ? s.name.text : "default";
        sigEntry(`${mod}.${name}`, s, f, sf, name, pub);
      } else if (ts.isVariableStatement(s)) {
        for (const d of s.declarationList.declarations) {
          const fn = ts.isIdentifier(d.name) && fnOf(d.initializer);
          if (fn) sigEntry(`${mod}.${d.name.text}`, fn, f, sf, d.name.text, pub);
        }
      } else if (ts.isClassDeclaration(s)) {
        const cls = s.name ? s.name.text : "default";
        for (const m of s.members) {
          if (!(ts.isMethodDeclaration(m) || ts.isConstructorDeclaration(m)) || !m.body) continue;
          const name = ts.isConstructorDeclaration(m) ? "constructor" : memberName(m);
          if (!name) continue;
          sigEntry(`${mod}.${cls}.${name}`, m, f, sf, name, pub && !hidden(m));
        }
      }
    }
    // Literals: string values compared, switched on, assigned or used as defaults.
    let n = 0;
    const lit = (value, node, context, field, target) => {
      if (n >= MAX_LITERALS || !value || value.length > 64 || /\s/.test(value)) return;
      n++;
      literals.push({ value, component: comp, file: relOf(f), line: lineOf(sf, node), context,
        ...(field ? { field } : {}), ...(target ? { target } : {}) });
    };
    const enumOf = (name, node, values) => {
      if (values.length >= 2) enums.push({ name, component: comp, file: relOf(f), line: lineOf(sf, node), values });
    };
    const unionValues = (t) => {
      t = t && ts.isParenthesizedTypeNode(t) ? t.type : t;
      if (!t || !ts.isUnionTypeNode(t)) return null;
      const vals = [];
      for (const x of t.types) {
        if (x.kind === ts.SyntaxKind.UndefinedKeyword || (ts.isLiteralTypeNode(x) && x.literal.kind === ts.SyntaxKind.NullKeyword)) continue;
        if (!(ts.isLiteralTypeNode(x) && ts.isStringLiteral(x.literal))) return null;
        vals.push(x.literal.text);
      }
      return vals;
    };
    const owner = (node) => {
      for (let p = node.parent; p; p = p.parent) {
        if ((ts.isInterfaceDeclaration(p) || ts.isTypeAliasDeclaration(p) || ts.isClassDeclaration(p)) && p.name) return p.name.text;
        if (ts.isFunctionLike(p)) return fnName(p);
      }
      return null;
    };
    const visit = (node) => {
      if (ts.isBinaryExpression(node)) {
        const op = node.operatorToken.kind;
        if ([ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsToken,
          ts.SyntaxKind.ExclamationEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken].includes(op)) {
          const l = strOf(node.left), r = strOf(node.right);
          if (ts.isTypeOfExpression(node.left) || ts.isTypeOfExpression(node.right)) { /* a JS type test */ }
          else if (r !== null) lit(r, node, "compare", nameOf(node.left));
          else if (l !== null) lit(l, node, "compare", nameOf(node.right));
        } else if (op === ts.SyntaxKind.EqualsToken && strOf(node.right) !== null
          && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))) {
          lit(strOf(node.right), node, "assign", nameOf(node.left));
        }
      } else if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
        && ["includes", "has"].includes(node.expression.name.text) && node.arguments.length === 1) {
        // ['a', 'b'].includes(x), new Set(['a', 'b']).has(x)
        let arr = node.expression.expression;
        if (ts.isNewExpression(arr) && arr.arguments?.length) arr = arr.arguments[0];
        if (ts.isArrayLiteralExpression(arr) && arr.elements.length && arr.elements.every((e) => strOf(e) !== null)) {
          for (const e of arr.elements) lit(e.text, e, "compare", nameOf(node.arguments[0]));
        }
      } else if (ts.isSwitchStatement(node)) {
        const field = nameOf(node.expression);
        for (const c of node.caseBlock.clauses) {
          if (ts.isCaseClause(c) && strOf(c.expression) !== null) lit(c.expression.text, c, "case", field);
        }
      } else if (ts.isPropertyAssignment(node) && strOf(node.initializer) !== null) {
        lit(node.initializer.text, node, "assign", memberName(node));
      } else if (ts.isPropertyDeclaration(node) && strOf(node.initializer) !== null) {
        lit(node.initializer.text, node, "assign", memberName(node));
      } else if ((ts.isParameter(node) || ts.isBindingElement(node)) && strOf(node.initializer) !== null) {
        const field = ts.isIdentifier(node.name) ? node.name.text : null;
        let fn = node.parent;
        while (fn && !ts.isFunctionLike(fn)) fn = fn.parent;
        lit(node.initializer.text, node, "default", field, fn ? fnName(fn) : null);
      } else if (ts.isTypeAliasDeclaration(node)) {
        const vals = unionValues(node.type);
        if (vals) enumOf(node.name.text, node, vals);
      } else if ((ts.isPropertySignature(node) || ts.isPropertyDeclaration(node) || ts.isParameter(node))
        && node.type && ts.isIdentifier(node.name)) {
        const vals = unionValues(node.type);
        const o = owner(node);
        if (vals) enumOf(o ? `${o}.${node.name.text}` : node.name.text, node, vals);
      } else if (ts.isEnumDeclaration(node)) {
        const vals = node.members.map((m) => strOf(m.initializer)).filter((v) => v !== null);
        if (vals.length === node.members.length) enumOf(node.name.text, node, vals);
      } else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer
        && ts.isAsExpression(node.initializer) && ts.isArrayLiteralExpression(node.initializer.expression)
        && node.initializer.type.getText() === "const") {
        // const STATUSES = ['open', 'closed'] as const
        const els = node.initializer.expression.elements;
        if (els.every((e) => strOf(e) !== null)) enumOf(node.name.text, node, els.map((e) => e.text));
        return;
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }

  // ----- dependencies and files -----
  const edges = new Map();
  const externals = new Map(); // id -> package name
  const extId = (name) => {
    const id = comps[name] ? `npm:${name}` : name;
    externals.set(id, name);
    return id;
  };
  const fileRefs = {};
  for (const f of files) {
    const recs = imports.get(f) || [];
    const a = compOf.get(f);
    const seen = new Set([f]);
    const refs = [];
    for (const r of recs) {
      if (r.file && !seen.has(r.file)) {
        seen.add(r.file);
        refs.push({ to: relOf(r.file), line: r.line });
      }
    }
    fileRefs[relOf(f)] = { component: a, refs };
    if (!counted(f)) continue;
    const perEdge = new Map(); // b -> {line, typeOnly}
    for (const r of recs) {
      const b = r.file ? compOf.get(r.file) : extId(r.ext);
      if (!b || b === a) continue;
      const e = perEdge.get(b);
      if (!e) perEdge.set(b, { line: r.line, typeOnly: r.typeOnly });
      else if (!r.typeOnly) e.typeOnly = false;
    }
    for (const [b, x] of perEdge) {
      const k = a + "\0" + b;
      if (!edges.has(k)) edges.set(k, { from: a, to: b, weight: 0, typeOnly: 0, evidence: [] });
      const e = edges.get(k);
      e.weight++;
      if (x.typeOnly) e.typeOnly++;
      if (e.evidence.length < EVIDENCE) e.evidence.push(`${relOf(f)}:${x.line}`);
    }
  }
  for (const [id, name] of [...externals].sort()) {
    comps[id] = { name, path: name === "node" ? "node:" : name, match: [name], kind: "external" };
  }
  const deps = [...edges.values()].sort((x, y) => x.from.localeCompare(y.from) || x.to.localeCompare(y.to))
    .map((e) => ({ from: e.from, to: e.to, weight: e.weight, ...(e.typeOnly ? { typeOnly: e.typeOnly } : {}),
      evidence: e.evidence }));

  // ----- source -----
  const ref = git(root, ["rev-parse", "--short", "HEAD"]) || "?";
  const status = git(root, ["status", "--porcelain", "--", rel(root, projectDir)]);
  const cd = path.resolve(projectDir) === path.resolve(root) ? "" : `cd ${rel(root, projectDir)} && `;
  const scripts = pkg.scripts || {};
  const commands = {};
  if (scripts.build) commands.build = `${cd}npm run build`;
  if (scripts.test) commands.test = `${cd}npm test`;
  const exts = new Set(files.map((f) => path.extname(f)));
  const languages = [];
  if ([".ts", ".tsx", ".mts", ".cts"].some((x) => exts.has(x)) || (cfg && cfg.config === "tsconfig.json")) languages.push("typescript");
  if ([".js", ".jsx", ".mjs", ".cjs"].some((x) => exts.has(x))) languages.push("javascript");
  const model = {
    format: "archmodel/1",
    source: {
      repo: path.basename(root), ref, dirty: !!status, languages, ...(pkg.name ? { module: pkg.name } : {}),
      generator: "archplan export-ts", generatedAt: new Date().toISOString().replace(/\.\d+Z$/, "Z"), commands,
      capabilities: ["components", "types", "fields", "deps", "files", "signatures", "literals"],
    },
    layers: [],
    components: Object.fromEntries(Object.entries(comps).sort((x, y) => x[0].localeCompare(y[0]))),
    dependencies: deps,
    files: Object.fromEntries(Object.entries(fileRefs).sort((x, y) => x[0].localeCompare(y[0]))),
    policy: { default: "allow", rules: [] },
    types: Object.fromEntries(Object.entries(types).sort((x, y) => x[0].localeCompare(y[0]))),
    sequences: {},
    signatures,
    literals,
    enums,
  };
  const text = JSON.stringify(model, null, 2) + "\n";
  const nInternal = Object.values(comps).filter((c) => c.kind === "internal").length;
  const summary = `${nInternal} components (+${externals.size} packages), ${deps.length} dependencies, `
    + `${Object.keys(types).length} types, ${signatures.length} signatures, ${literals.length} literals, `
    + `${enums.length} enums, ${files.length} files (typescript ${ts.version}, `
    + `${cfg ? cfg.config : "no tsconfig"}, ${((Date.now() - t0) / 1000).toFixed(1)}s)`;
  if (opt.out) {
    fs.mkdirSync(path.dirname(path.resolve(opt.out)), { recursive: true });
    fs.writeFileSync(opt.out, text);
    process.stderr.write(`${opt.out}: ${summary}\n`);
  } else {
    process.stdout.write(text);
    process.stderr.write(summary + "\n");
  }
  if (unresolved.size) {
    const ex = [...unresolved].slice(0, 3).map(([s, f]) => `${s} (${relOf(f)})`).join(", ");
    process.stderr.write(`${unresolved.size} imports did not resolve, such as ${ex}\n`);
  }
}

main();
