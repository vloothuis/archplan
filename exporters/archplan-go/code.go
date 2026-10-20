package main

import (
	"go/ast"
	"go/constant"
	"go/token"
	"go/types"
	"maps"
	"slices"
	"strconv"
	"strings"
	"unicode"
)

// Limits for the literals section, as the format sets them.
const (
	maxLiteralLen      = 64
	maxLiteralsPerFile = 200
)

// code adds the sections that come from reading function bodies: file
// references, signatures, literals and HTTP routes. It needs the type
// information check recorded.
func (x *extractor) code() {
	x.res.Files = map[string]File{}
	for _, p := range x.pkgs {
		for _, f := range p.files {
			x.res.Files[x.file(f.Pos())] = File{Component: p.comp, Refs: []Ref{}}
		}
	}
	for _, p := range x.pkgs {
		if p.info == nil {
			continue
		}
		for _, f := range p.files {
			x.fileRefs(p, f)
			x.signatures(p, f)
			x.literals(p, f)
			x.routes(p, f)
		}
	}
}

// file is the module-relative path of the file at pos.
func (x *extractor) file(pos token.Pos) string {
	w := x.where(pos)
	return w[:strings.LastIndex(w, ":")]
}

func (x *extractor) line(pos token.Pos) int { return x.fset.Position(pos).Line }

// fileRefs lists the other module files a file refers to: the files that
// declare the identifiers it uses, from its own package and from the
// packages it imports. An import of a module package whose identifiers do
// not resolve, such as a blank import, refers to all of that package's files.
func (x *extractor) fileRefs(p *goPkg, f *ast.File) {
	self := x.file(f.Pos())
	first := map[string]int{}
	add := func(to string, line int) {
		if to == self {
			return
		}
		if _, ok := x.res.Files[to]; !ok {
			return
		}
		if l, ok := first[to]; !ok || line < l {
			first[to] = line
		}
	}
	resolved := map[string]bool{} // import paths with at least one resolved use
	ast.Inspect(f, func(n ast.Node) bool {
		id, ok := n.(*ast.Ident)
		if !ok {
			return true
		}
		obj := p.info.Uses[id]
		if obj == nil || obj.Pkg() == nil || !obj.Pos().IsValid() {
			return true
		}
		if _, ok := obj.(*types.PkgName); ok {
			return true
		}
		add(x.file(obj.Pos()), x.line(id.Pos()))
		resolved[obj.Pkg().Path()] = true
		return true
	})
	byPath := map[string]*goPkg{}
	for _, q := range x.pkgs {
		byPath[q.ImportPath] = q
	}
	for _, imp := range f.Imports {
		path, err := strconv.Unquote(imp.Path.Value)
		if err != nil || resolved[path] {
			continue
		}
		if q := byPath[path]; q != nil {
			for _, qf := range q.files {
				add(x.file(qf.Pos()), x.line(imp.Pos()))
			}
		}
	}
	refs := make([]Ref, 0, len(first))
	for _, to := range slices.Sorted(maps.Keys(first)) {
		refs = append(refs, Ref{To: to, Line: first[to]})
	}
	x.res.Files[self] = File{Component: p.comp, Refs: refs}
}

// funcID is the signature ID of a function or method: the module-relative
// package path, a dot, and the name, with the receiver for a method:
// internal/store.(*Store).Get, internal/store.Value.String, internal/store.New.
// Functions of the module's root package have no package part.
func funcID(rel string, fn *types.Func) string {
	name := fn.Name()
	if sig, ok := fn.Type().(*types.Signature); ok && sig.Recv() != nil {
		t := sig.Recv().Type()
		ptr := false
		if pt, ok := t.(*types.Pointer); ok {
			ptr, t = true, pt.Elem()
		}
		recv := "?"
		if n, ok := types.Unalias(t).(*types.Named); ok {
			recv = n.Obj().Name()
		}
		if ptr {
			recv = "(*" + recv + ")"
		}
		name = recv + "." + name
	}
	if rel == "." {
		return name
	}
	return rel + "." + name
}

// signatures adds the file's functions and methods. init functions are left
// out: a package can have several, and nothing calls them.
func (x *extractor) signatures(p *goPkg, f *ast.File) {
	qual := func(q *types.Package) string {
		if q == p.types {
			return ""
		}
		return q.Name()
	}
	for _, d := range f.Decls {
		fd, ok := d.(*ast.FuncDecl)
		if !ok || fd.Name.Name == "_" || (fd.Recv == nil && fd.Name.Name == "init") {
			continue
		}
		fn, ok := p.info.Defs[fd.Name].(*types.Func)
		if !ok {
			continue
		}
		sig, ok := fn.Type().(*types.Signature)
		if !ok {
			continue
		}
		s := Signature{
			ID: funcID(p.rel, fn), Component: p.comp, File: x.file(fd.Pos()), Line: x.line(fd.Name.Pos()),
			Name: fn.Name(), Params: []Param{}, Public: fn.Exported(),
		}
		if sig.Recv() != nil {
			s.Public = s.Public && recvExported(sig.Recv().Type())
		}
		for i := range sig.Params().Len() {
			v := sig.Params().At(i)
			typ := types.TypeString(v.Type(), qual)
			if sig.Variadic() && i == sig.Params().Len()-1 {
				if sl, ok := v.Type().(*types.Slice); ok {
					typ = "..." + types.TypeString(sl.Elem(), qual)
				}
			}
			name := v.Name()
			if name == "" {
				name = "_"
			}
			s.Params = append(s.Params, Param{Name: name, Type: typ})
		}
		if sig.Results().Len() > 0 {
			r := types.TypeString(sig.Results(), qual)
			if sig.Results().Len() == 1 && sig.Results().At(0).Name() == "" {
				r = strings.TrimSuffix(strings.TrimPrefix(r, "("), ")")
			}
			s.Returns = &r
		}
		x.res.Signatures = append(x.res.Signatures, s)
	}
}

func recvExported(t types.Type) bool {
	if pt, ok := t.(*types.Pointer); ok {
		t = pt.Elem()
	}
	if n, ok := types.Unalias(t).(*types.Named); ok {
		return n.Obj().Exported()
	}
	return true
}

// literals adds the string values the file compares (== and !=), switches
// on (case), and assigns to a field or key (assign). Like the other
// exporters, it keeps values without whitespace of at most 64 characters,
// and at most 200 per file. Test files are not in the package's GoFiles.
func (x *extractor) literals(p *goPkg, f *ast.File) {
	file := x.file(f.Pos())
	n := 0
	add := func(e ast.Expr, context, field string) {
		lit, ok := e.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING || n >= maxLiteralsPerFile {
			return
		}
		v, err := strconv.Unquote(lit.Value)
		if err != nil || v == "" || len(v) > maxLiteralLen || strings.IndexFunc(v, unicode.IsSpace) >= 0 {
			return
		}
		n++
		x.res.Literals = append(x.res.Literals, Literal{
			Value: v, Component: p.comp, File: file, Line: x.line(lit.Pos()), Context: context, Field: field,
		})
	}
	ast.Inspect(f, func(node ast.Node) bool {
		switch e := node.(type) {
		case *ast.BinaryExpr:
			if e.Op == token.EQL || e.Op == token.NEQ {
				add(e.Y, "compare", nameOf(e.X))
				add(e.X, "compare", nameOf(e.Y))
			}
		case *ast.SwitchStmt:
			if e.Tag != nil {
				field := nameOf(e.Tag)
				for _, s := range e.Body.List {
					if cc, ok := s.(*ast.CaseClause); ok {
						for _, v := range cc.List {
							add(v, "case", field)
						}
					}
				}
			}
		case *ast.AssignStmt:
			if len(e.Lhs) == len(e.Rhs) {
				for i, l := range e.Lhs {
					switch l.(type) {
					case *ast.SelectorExpr, *ast.IndexExpr:
						add(e.Rhs[i], "assign", nameOf(l))
					}
				}
			}
		case *ast.KeyValueExpr:
			if k := nameOf(e.Key); k != "" {
				add(e.Value, "assign", k)
			}
		}
		return true
	})
}

// nameOf is the name a literal is compared with or assigned to: status,
// x.status, m["status"], status().
func nameOf(e ast.Expr) string {
	switch e := e.(type) {
	case *ast.Ident:
		return e.Name
	case *ast.SelectorExpr:
		return e.Sel.Name
	case *ast.IndexExpr:
		if lit, ok := e.Index.(*ast.BasicLit); ok && lit.Kind == token.STRING {
			if s, err := strconv.Unquote(lit.Value); err == nil {
				return s
			}
		}
	case *ast.BasicLit:
		if e.Kind == token.STRING {
			if s, err := strconv.Unquote(e.Value); err == nil {
				return s
			}
		}
	case *ast.CallExpr:
		return nameOf(e.Fun)
	case *ast.ParenExpr:
		return nameOf(e.X)
	case *ast.StarExpr:
		return nameOf(e.X)
	}
	return ""
}

// routes adds the routes the file registers on a net/http ServeMux, with
// Handle or HandleFunc, on a mux or on the default one. The pattern must be
// a constant. The handler is the function or method passed, when it is one
// of the module's; a function literal leaves the handler out and gives the
// literal's place.
func (x *extractor) routes(p *goPkg, f *ast.File) {
	ast.Inspect(f, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || len(call.Args) != 2 {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || (sel.Sel.Name != "Handle" && sel.Sel.Name != "HandleFunc") {
			return true
		}
		fn, ok := p.info.Uses[sel.Sel].(*types.Func)
		if !ok || fn.Pkg() == nil || fn.Pkg().Path() != "net/http" {
			return true
		}
		tv, ok := p.info.Types[call.Args[0]]
		if !ok || tv.Value == nil || tv.Value.Kind() != constant.String {
			return true
		}
		method, pattern := splitPattern(constant.StringVal(tv.Value))
		r := Route{Method: method, Pattern: pattern, Component: p.comp, File: x.file(call.Pos()), Line: x.line(call.Pos())}
		if h := x.handlerFunc(p, call.Args[1]); h != nil {
			if rel, ok := x.ix.rel(h.Pkg().Path()); ok {
				r.Handler = funcID(rel, h)
				if h.Pos().IsValid() {
					if file := x.file(h.Pos()); x.res.Files[file].Component != "" {
						r.File, r.Line, r.Component = file, x.line(h.Pos()), x.res.Files[file].Component
					}
				}
			}
		} else if lit, ok := call.Args[1].(*ast.FuncLit); ok {
			r.Line = x.line(lit.Pos())
		}
		x.res.Routes = append(x.res.Routes, r)
		return true
	})
}

// handlerFunc is the function or method a handler expression names: f,
// a.f, or a conversion such as http.HandlerFunc(a.f).
func (x *extractor) handlerFunc(p *goPkg, e ast.Expr) *types.Func {
	for {
		switch v := e.(type) {
		case *ast.ParenExpr:
			e = v.X
			continue
		case *ast.CallExpr:
			if tv, ok := p.info.Types[v.Fun]; ok && tv.IsType() && len(v.Args) == 1 {
				e = v.Args[0]
				continue
			}
			return nil
		case *ast.Ident:
			fn, _ := p.info.Uses[v].(*types.Func)
			return fn
		case *ast.SelectorExpr:
			fn, _ := p.info.Uses[v.Sel].(*types.Func)
			return fn
		}
		return nil
	}
}

// splitPattern splits a ServeMux pattern, "[METHOD ][HOST]/[PATH]", into
// the method ("*" for any) and the path. The host is dropped.
func splitPattern(s string) (method, path string) {
	method = "*"
	s = strings.TrimSpace(s)
	if m, rest, ok := strings.Cut(s, " "); ok {
		method, s = strings.ToUpper(m), strings.TrimSpace(rest)
	}
	if i := strings.Index(s, "/"); i > 0 {
		s = s[i:]
	}
	return method, s
}
