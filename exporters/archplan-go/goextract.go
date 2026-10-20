package main

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"maps"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
)

// GoOptions controls what ExtractGo puts in the model.
type GoOptions struct {
	Root       string   // module root; file paths in the model are relative to it
	Module     string   // module path
	Exclude    []string // module-relative directories to skip, as in go-arch-lint's exclude
	Depth      int      // group uncovered packages by this many leading path segments; 0 is one per package
	Layer      string   // layer of components added for uncovered packages
	Unexported bool     // also export unexported types and methods
	Vocab      bool     // also build the vocabulary section
}

// GoResult is what ExtractGo found.
type GoResult struct {
	Unmapped     []string // module packages no component covered; ExtractGo added components for them
	Dependencies []Dependency
	Types        map[string]Type
	TypeErrors   int
	Vocabulary   *Vocabulary
	Files        map[string]File
	Signatures   []Signature
	Literals     []Literal
	Routes       []Route
	Warnings     []string
}

// maxEvidence bounds the file:line examples kept per dependency.
const maxEvidence = 3

type goPkg struct {
	Package
	rel   string
	comp  string
	files []*ast.File
	types *types.Package
	info  *types.Info // uses, definitions and expression types
}

type extractor struct {
	opt   GoOptions
	ix    *componentIndex
	fset  *token.FileSet
	pkgs  []*goPkg
	model map[string]*types.TypeName // type ID → object, for the types in the model
	docs  map[token.Pos]string
	res   GoResult
}

// ExtractGo type-checks the module's own packages from source, and reads
// everything else from the export data `go list -export` produced. It adds a
// component for each module package that no component covers.
func ExtractGo(list []Package, comps map[string]Component, opt GoOptions) (*GoResult, error) {
	x := &extractor{
		opt: opt, ix: newComponentIndex(opt.Module, comps), fset: token.NewFileSet(),
		model: map[string]*types.TypeName{}, docs: map[token.Pos]string{},
	}
	x.res.Types = map[string]Type{}
	x.selectPackages(list, comps)
	if err := x.parse(); err != nil {
		return nil, err
	}
	x.check(list)
	x.dependencies()
	x.collectTypes()
	for id, obj := range x.model {
		x.res.Types[id] = x.describe(obj)
	}
	x.implementations()
	x.code()
	if opt.Vocab {
		x.vocabulary()
	}
	return &x.res, nil
}

func (x *extractor) excluded(rel string) bool {
	for _, e := range x.opt.Exclude {
		if rel == e || strings.HasPrefix(rel, e+"/") {
			return true
		}
	}
	return false
}

// selectPackages picks the module packages named on the command line, in
// dependency order, and assigns each to a component. A package no component
// covers gets a component named by its module-relative path, or by the first
// opt.Depth segments of that path.
func (x *extractor) selectPackages(list []Package, comps map[string]Component) {
	for _, p := range list {
		rel, ok := x.ix.rel(p.ImportPath)
		if !ok || p.DepOnly || p.Standard || x.excluded(rel) {
			continue
		}
		comp := x.ix.of(p.ImportPath)
		if comp == "" {
			comp = groupOf(rel, x.opt.Depth)
			comps[comp] = x.addToGroup(comps[comp], comp, rel)
			x.ix.cache[p.ImportPath] = comp
			x.res.Unmapped = append(x.res.Unmapped, rel)
		}
		x.pkgs = append(x.pkgs, &goPkg{Package: p, rel: rel, comp: comp})
	}
}

// addToGroup returns the component for the group dir after adding the
// package rel to it. A group matches its own directory and, once it holds a
// package below that directory, everything below it.
func (x *extractor) addToGroup(c Component, dir, rel string) Component {
	if c.Name == "" {
		c = Component{Name: dir, Path: dir, Layer: x.opt.Layer, Kind: KindInternal}
		if dir == "." {
			c.Name = path.Base(x.opt.Module)
		}
	}
	pattern := dir
	if dir != rel {
		pattern = dir + "/**"
	}
	if !slices.Contains(c.Match, pattern) {
		c.Match = append(c.Match, pattern)
		slices.Sort(c.Match)
	}
	return c
}

// groupOf returns the first depth segments of a module-relative path, or the
// whole path when depth is 0 or the path is shorter.
func groupOf(rel string, depth int) string {
	if depth <= 0 || rel == "." {
		return rel
	}
	segs := strings.Split(rel, "/")
	return strings.Join(segs[:min(depth, len(segs))], "/")
}

// UnmappedLayer holds components ExtractGo added for packages a go-arch-lint
// config does not cover.
const UnmappedLayer = "unmapped"

// typeID is the model ID of a type: the module-relative package path, a dot,
// and the type name. Types in the module's root package use the name alone.
func typeID(rel, name string) string {
	if rel == "." {
		return name
	}
	return rel + "." + name
}

func (x *extractor) parse() error {
	for _, p := range x.pkgs {
		for _, name := range append(slices.Clone(p.GoFiles), p.CgoFiles...) {
			f, err := parser.ParseFile(x.fset, filepath.Join(p.Dir, name), nil, parser.ParseComments)
			if err != nil {
				return fmt.Errorf("parse %s: %w", name, err)
			}
			p.files = append(p.files, f)
			x.collectDocs(f)
		}
	}
	return nil
}

// collectDocs records the first sentence of each type's doc comment, keyed by
// the position of its name.
func (x *extractor) collectDocs(f *ast.File) {
	for _, d := range f.Decls {
		g, ok := d.(*ast.GenDecl)
		if !ok || g.Tok != token.TYPE {
			continue
		}
		for _, s := range g.Specs {
			ts, ok := s.(*ast.TypeSpec)
			if !ok {
				continue
			}
			doc := ts.Doc
			if doc == nil && len(g.Specs) == 1 {
				doc = g.Doc
			}
			if doc != nil {
				x.docs[ts.Name.Pos()] = firstSentence(doc.Text())
			}
		}
	}
}

func firstSentence(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	if i := strings.Index(s, ". "); i >= 0 {
		return s[:i+1]
	}
	return s
}

// moduleImporter serves the module's packages from the source-checked set, so
// a type has one identity everywhere, and the rest from export data.
type moduleImporter struct {
	local map[string]*types.Package
	gc    types.ImporterFrom
}

func (im *moduleImporter) Import(path string) (*types.Package, error) {
	return im.ImportFrom(path, "", 0)
}

func (im *moduleImporter) ImportFrom(path, dir string, mode types.ImportMode) (*types.Package, error) {
	if p, ok := im.local[path]; ok {
		return p, nil
	}
	p, err := im.gc.ImportFrom(path, dir, mode)
	if err != nil {
		return nil, fmt.Errorf("import %s: %w", path, err)
	}
	return p, nil
}

func (x *extractor) check(list []Package) {
	exports := map[string]string{}
	for _, p := range list {
		if p.Export != "" {
			exports[p.ImportPath] = p.Export
		}
	}
	lookup := func(path string) (io.ReadCloser, error) {
		f, ok := exports[path]
		if !ok {
			return nil, fmt.Errorf("no export data for %s", path)
		}
		return os.Open(f)
	}
	gc, ok := importer.ForCompiler(x.fset, "gc", lookup).(types.ImporterFrom)
	if !ok {
		panic("gc importer does not implement types.ImporterFrom")
	}
	im := &moduleImporter{local: map[string]*types.Package{}, gc: gc}
	for _, p := range x.pkgs {
		p.info = &types.Info{
			Uses: map[*ast.Ident]types.Object{}, Defs: map[*ast.Ident]types.Object{},
			Types: map[ast.Expr]types.TypeAndValue{},
		}
		conf := types.Config{Importer: im, FakeImportC: true, Error: func(error) { x.res.TypeErrors++ }}
		// Type errors are counted, not fatal: the partial package is still
		// worth describing, and the count tells the caller to look.
		p.types, _ = conf.Check(p.ImportPath, x.fset, p.files, p.info)
		im.local[p.ImportPath] = p.types
	}
}

// dependencies counts, per component pair, the files whose imports cross it.
func (x *extractor) dependencies() {
	type pair struct{ from, to string }
	deps := map[pair]*Dependency{}
	for _, p := range x.pkgs {
		for _, f := range p.files {
			seen := map[string]bool{}
			for _, imp := range f.Imports {
				path, err := strconv.Unquote(imp.Path.Value)
				if err != nil {
					continue
				}
				to := x.ix.of(path)
				if to == "" || to == p.comp || seen[to] {
					continue
				}
				seen[to] = true
				d := deps[pair{p.comp, to}]
				if d == nil {
					d = &Dependency{From: p.comp, To: to}
					deps[pair{p.comp, to}] = d
				}
				d.Weight++
				if len(d.Evidence) < maxEvidence {
					d.Evidence = append(d.Evidence, x.where(imp.Pos()))
				}
			}
		}
	}
	for _, k := range slices.SortedFunc(maps.Keys(deps), func(a, b pair) int {
		return strings.Compare(a.from+" "+a.to, b.from+" "+b.to)
	}) {
		x.res.Dependencies = append(x.res.Dependencies, *deps[k])
	}
}

func (x *extractor) where(pos token.Pos) string {
	p := x.fset.Position(pos)
	rel, err := filepath.Rel(x.opt.Root, p.Filename)
	if err != nil {
		rel = p.Filename
	}
	return filepath.ToSlash(rel) + ":" + strconv.Itoa(p.Line)
}

func (x *extractor) collectTypes() {
	for _, p := range x.pkgs {
		if p.types == nil {
			continue
		}
		scope := p.types.Scope()
		for _, name := range scope.Names() {
			tn, ok := scope.Lookup(name).(*types.TypeName)
			if !ok || (!tn.Exported() && !x.opt.Unexported) {
				continue
			}
			x.model[typeID(p.rel, name)] = tn
		}
	}
}

// idOf returns the model ID of a type, if the type is in the model.
func (x *extractor) idOf(tn *types.TypeName) (string, bool) {
	if tn.Pkg() == nil {
		return "", false
	}
	rel, ok := x.ix.rel(tn.Pkg().Path())
	if !ok {
		return "", false
	}
	id := typeID(rel, tn.Name())
	if x.model[id] != tn {
		return "", false
	}
	return id, true
}

func (x *extractor) compOf(tn *types.TypeName) string {
	return x.ix.of(tn.Pkg().Path())
}

func (x *extractor) describe(tn *types.TypeName) Type {
	t := Type{Name: tn.Name(), Component: x.compOf(tn), Doc: x.docs[tn.Pos()]}
	pos := x.where(tn.Pos())
	if i := strings.LastIndex(pos, ":"); i > 0 {
		t.File = pos[:i]
		t.Line, _ = strconv.Atoi(pos[i+1:])
	}
	qual := func(p *types.Package) string {
		if p == tn.Pkg() {
			return ""
		}
		return p.Name()
	}
	if tn.IsAlias() {
		// An alias declares no type of its own; it depends on its target.
		t.Kind = "alias"
		if id := x.refOf(types.Unalias(tn.Type())); id != "" {
			t.Uses = []string{id}
		}
		return t
	}
	held := map[string]bool{}
	switch u := tn.Type().Underlying().(type) {
	case *types.Struct:
		t.Kind = "struct"
		t.Fields = x.fields(u, qual, held)
	case *types.Interface:
		t.Kind = "interface"
		for et := range u.EmbeddedTypes() {
			if id := x.refOf(et); id != "" {
				t.Implements = append(t.Implements, id)
			}
		}
		for m := range u.ExplicitMethods() {
			t.Methods = x.addMethod(t.Methods, m, qual)
		}
	case *types.Signature:
		t.Kind = "function"
	default:
		t.Kind = x.basicKind(tn)
	}
	if named, ok := tn.Type().(*types.Named); ok && t.Kind != "interface" {
		for m := range named.Methods() {
			t.Methods = x.addMethod(t.Methods, m, qual)
		}
	}
	t.Uses = x.uses(tn, held)
	return t
}

// basicKind tells an enum (a defined type with constants) from a plain
// defined type, which the format calls an alias.
func (x *extractor) basicKind(tn *types.TypeName) string {
	scope := tn.Pkg().Scope()
	for _, name := range scope.Names() {
		if c, ok := scope.Lookup(name).(*types.Const); ok && types.Identical(c.Type(), tn.Type()) {
			return "enum"
		}
	}
	return "alias"
}

func (x *extractor) addMethod(ms []Method, f *types.Func, qual types.Qualifier) []Method {
	if !f.Exported() && !x.opt.Unexported {
		return ms
	}
	sig, ok := f.Type().(*types.Signature)
	if !ok {
		return ms
	}
	var b bytes.Buffer
	types.WriteSignature(&b, sig, qual)
	return append(ms, Method{Name: f.Name(), Sig: b.String()})
}

func (x *extractor) fields(st *types.Struct, qual types.Qualifier, held map[string]bool) []Field {
	var out []Field
	for f := range st.Fields() {
		if f.Name() == "_" {
			continue
		}
		ref, ptr, many := x.fieldRef(f.Type())
		if ref == "" {
			out = append(out, Field{Name: f.Name(), Type: types.TypeString(f.Type(), qual)})
			continue
		}
		held[ref] = true
		out = append(out, Field{Name: f.Name(), Ref: ref, Ptr: ptr, Many: many})
	}
	return out
}

// fieldRef finds the model type a field holds, looking through pointers,
// collections and generic wrappers such as atomic.Pointer[T].
func (x *extractor) fieldRef(t types.Type) (ref string, ptr, many bool) {
	for {
		// A field declared with an alias depends on the alias's package,
		// not on the package of the aliased type.
		if a, ok := t.(*types.Alias); ok {
			if id, ok := x.idOf(a.Obj()); ok {
				return id, ptr, many
			}
		}
		switch u := types.Unalias(t).(type) {
		case *types.Pointer:
			ptr = ptr || !many
			t = u.Elem()
		case *types.Slice:
			many, t = true, u.Elem()
		case *types.Array:
			many, t = true, u.Elem()
		case *types.Map:
			many, t = true, u.Elem()
		case *types.Chan:
			many, t = true, u.Elem()
		case *types.Named:
			if id, ok := x.idOf(u.Origin().Obj()); ok {
				return id, ptr, many
			}
			for arg := range u.TypeArgs().Types() {
				if id, _, m := x.fieldRef(arg); id != "" {
					return id, true, many || m
				}
			}
			return "", false, false
		default:
			return "", false, false
		}
	}
}

func (x *extractor) refOf(t types.Type) string {
	id, _, _ := x.fieldRef(t)
	return id
}

// uses lists model types in method signatures that no field already holds.
func (x *extractor) uses(tn *types.TypeName, held map[string]bool) []string {
	self, _ := x.idOf(tn)
	found := map[string]bool{}
	visit := func(sig *types.Signature) {
		for _, tup := range []*types.Tuple{sig.Params(), sig.Results()} {
			for v := range tup.Variables() {
				if id := x.refOf(v.Type()); id != "" && id != self && !held[id] {
					found[id] = true
				}
			}
		}
	}
	if iface, ok := tn.Type().Underlying().(*types.Interface); ok {
		for m := range iface.ExplicitMethods() {
			if s, ok := m.Type().(*types.Signature); ok {
				visit(s)
			}
		}
	} else if named, ok := tn.Type().(*types.Named); ok {
		for m := range named.Methods() {
			if m.Exported() || x.opt.Unexported {
				if s, ok := m.Type().(*types.Signature); ok {
					visit(s)
				}
			}
		}
	}
	return slices.Sorted(maps.Keys(found))
}

// implementations adds structural "implements" edges from every concrete
// model type to every non-empty model interface its method set satisfies.
func (x *extractor) implementations() {
	type iface struct {
		id    string
		typ   *types.Interface
		names []string
	}
	var ifaces []iface
	for id, tn := range x.model {
		it, ok := tn.Type().Underlying().(*types.Interface)
		if !ok || it.NumMethods() == 0 || isGeneric(tn) {
			continue
		}
		names := make([]string, it.NumMethods())
		for i := range it.NumMethods() {
			names[i] = it.Method(i).Name()
		}
		ifaces = append(ifaces, iface{id, it, names})
	}
	for id, tn := range x.model {
		if _, ok := tn.Type().Underlying().(*types.Interface); ok || isGeneric(tn) {
			continue
		}
		ptr := types.NewPointer(tn.Type())
		ms := types.NewMethodSet(ptr)
		var impl []string
		for _, in := range ifaces {
			if !hasAll(ms, tn.Pkg(), in.names) {
				continue
			}
			if types.Implements(tn.Type(), in.typ) || types.Implements(ptr, in.typ) {
				impl = append(impl, in.id)
			}
		}
		if len(impl) > 0 {
			t := x.res.Types[id]
			t.Implements = slices.Sorted(slices.Values(impl))
			x.res.Types[id] = t
		}
	}
}

func isGeneric(tn *types.TypeName) bool {
	n, ok := tn.Type().(*types.Named)
	return ok && n.TypeParams().Len() > 0
}

// hasAll is a cheap pre-check before types.Implements.
func hasAll(ms *types.MethodSet, pkg *types.Package, names []string) bool {
	for _, n := range names {
		if ms.Lookup(pkg, n) == nil && ms.Lookup(nil, n) == nil {
			return false
		}
	}
	return true
}
