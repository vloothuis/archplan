package main

// Format is the archmodel version this package writes.
const Format = "archmodel/1"

// Model is an archmodel v1 document.
type Model struct {
	Format       string               `json:"format"`
	Source       Source               `json:"source"`
	Layers       []Layer              `json:"layers,omitempty"`
	Components   map[string]Component `json:"components"`
	Dependencies []Dependency         `json:"dependencies"`
	Files        map[string]File      `json:"files,omitempty"`
	Policy       *Policy              `json:"policy,omitempty"`
	Types        map[string]Type      `json:"types,omitempty"`
	Sequences    map[string]Sequence  `json:"sequences,omitempty"`
	Signatures   []Signature          `json:"signatures,omitempty"`
	Literals     []Literal            `json:"literals,omitempty"`
	Routes       []Route              `json:"routes,omitempty"`
	Vocabulary   *Vocabulary          `json:"vocabulary,omitempty"`
	Notes        map[string]string    `json:"notes,omitempty"`
}

// Source says where a model came from and how to check work against it.
type Source struct {
	Repo        string            `json:"repo,omitempty"`
	Ref         string            `json:"ref,omitempty"`
	Dirty       bool              `json:"dirty,omitempty"`
	Languages   []string          `json:"languages,omitempty"`
	Module      string            `json:"module,omitempty"`
	Generator   string            `json:"generator,omitempty"`
	GeneratedAt string            `json:"generatedAt,omitempty"`
	Commands    map[string]string `json:"commands,omitempty"`
	// Capabilities lists what the model contains; see exporter-plugins.md.
	Capabilities []string `json:"capabilities,omitempty"`
}

// Layer is an ordered group of components, outermost first.
type Layer struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// Component is the unit the dependency policy speaks about.
type Component struct {
	Name  string   `json:"name"`
	Path  string   `json:"path,omitempty"`
	Match []string `json:"match,omitempty"`
	Layer string   `json:"layer,omitempty"`
	Kind  string   `json:"kind,omitempty"`
	Doc   string   `json:"doc,omitempty"`
}

// Component kinds.
const (
	KindInternal = "internal"
	KindExternal = "external"
)

// Dependency is an observed dependency between two components.
type Dependency struct {
	From     string   `json:"from"`
	To       string   `json:"to"`
	Weight   int      `json:"weight"`
	Evidence []string `json:"evidence,omitempty"`
}

// Policy lists allowed and forbidden dependencies.
type Policy struct {
	Default  string    `json:"default"`
	Rules    []Rule    `json:"rules"`
	Enforcer *Enforcer `json:"enforcer,omitempty"`
}

// Rule allows or forbids dependencies from one component to another. "*"
// matches any component.
type Rule struct {
	From   string `json:"from"`
	To     string `json:"to"`
	Effect string `json:"effect"`
	Note   string `json:"note,omitempty"`
}

// Rule effects, also used as policy defaults.
const (
	Allow = "allow"
	Deny  = "deny"
)

// Enforcer names the tool that checks the policy in CI.
type Enforcer struct {
	Tool    string `json:"tool"`
	Config  string `json:"config,omitempty"`
	Command string `json:"command,omitempty"`
}

// Type is a named type and its relations to other types in the model.
type Type struct {
	Name       string   `json:"name"`
	Component  string   `json:"component"`
	Kind       string   `json:"kind"`
	File       string   `json:"file,omitempty"`
	Line       int      `json:"line,omitempty"`
	Doc        string   `json:"doc,omitempty"`
	Fields     []Field  `json:"fields,omitempty"`
	Methods    []Method `json:"methods,omitempty"`
	Implements []string `json:"implements,omitempty"`
	Uses       []string `json:"uses,omitempty"`
}

// Field is a struct field. Ref is set when the field's type is in the model.
type Field struct {
	Name string `json:"name"`
	Type string `json:"type,omitempty"`
	Ref  string `json:"ref,omitempty"`
	Ptr  bool   `json:"ptr,omitempty"`
	Many bool   `json:"many,omitempty"`
}

// Method is a method with its signature in Go syntax, without "func".
type Method struct {
	Name string `json:"name"`
	Sig  string `json:"sig,omitempty"`
}

// Sequence is the ordered calls of one traced scenario.
type Sequence struct {
	Title        string              `json:"title,omitempty"`
	Entry        string              `json:"entry,omitempty"`
	Source       string              `json:"source,omitempty"`
	Participants []string            `json:"participants"`
	Fragments    map[string]Fragment `json:"fragments,omitempty"`
	Messages     []Message           `json:"messages"`
}

// Fragment is a loop around some messages.
type Fragment struct {
	Kind   string  `json:"kind"`
	Label  string  `json:"label,omitempty"`
	Parent *string `json:"parent"`
}

// Message is one call. Depth is the call depth; a message directly after one
// with a lower depth is a call made inside it.
type Message struct {
	ID       string `json:"id"`
	From     string `json:"from"`
	To       string `json:"to"`
	Label    string `json:"label"`
	Depth    int    `json:"depth"`
	Fragment string `json:"fragment,omitempty"`
	Async    bool   `json:"async,omitempty"`
	Via      string `json:"via,omitempty"`
	// Outcome is the kind of a refusing return: error, false or status:<4xx>.
	Outcome string `json:"outcome,omitempty"`
	// Route is the HTTP route a call with Via "http" requests.
	Route *HTTPRoute `json:"route,omitempty"`
	// Span correlates a client call with the server trace it caused.
	Span string `json:"span,omitempty"`
}

// HTTPRoute is the method and path pattern of an HTTP request.
type HTTPRoute struct {
	Method string `json:"method,omitempty"`
	Path   string `json:"path"`
}

// File is one source file and the other module files it refers to.
type File struct {
	Component string `json:"component"`
	Refs      []Ref  `json:"refs"`
}

// Ref is a reference to another module file, at the line of the first use.
type Ref struct {
	To   string `json:"to"`
	Line int    `json:"line,omitempty"`
}

// Signature is a function or method with its parameters.
type Signature struct {
	ID        string  `json:"id"`
	Component string  `json:"component"`
	File      string  `json:"file"`
	Line      int     `json:"line"`
	Name      string  `json:"name"`
	Params    []Param `json:"params"`
	Returns   *string `json:"returns"`
	Public    bool    `json:"public"`
}

// Param is one parameter of a signature.
type Param struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// Literal is a string value the code compares, switches on or assigns.
type Literal struct {
	Value     string `json:"value"`
	Component string `json:"component"`
	File      string `json:"file"`
	Line      int    `json:"line"`
	Context   string `json:"context"`
	Field     string `json:"field,omitempty"`
}

// Route is an HTTP route the code registers, and the function that serves it.
type Route struct {
	Method    string `json:"method"`
	Pattern   string `json:"pattern"`
	Handler   string `json:"handler,omitempty"`
	Component string `json:"component,omitempty"`
	File      string `json:"file,omitempty"`
	Line      int    `json:"line,omitempty"`
}

// Vocabulary is the domain language of the code: the concepts each component
// owns, which concepts it may or must not name, and where code names a concept
// it does not own.
type Vocabulary struct {
	Concepts   map[string]Concept        `json:"concepts,omitempty"`
	Components map[string]ComponentTerms `json:"components,omitempty"`
	Usage      []Usage                   `json:"usage,omitempty"`
}

// Concept is a named idea that one component owns.
type Concept struct {
	Name       string   `json:"name"`
	Owner      string   `json:"owner,omitempty"`
	Terms      []string `json:"terms,omitempty"`
	Kind       string   `json:"kind,omitempty"`
	Definition string   `json:"definition,omitempty"`
	Source     string   `json:"source,omitempty"`
}

// Concept kinds and sources.
const (
	ConceptDomain    = "domain"
	ConceptVendor    = "vendor"
	ConceptTechnical = "technical"

	SourceGlossary  = "glossary"
	SourceVendor    = "vendor"
	SourceSuggested = "suggested"
)

// ComponentTerms is one component's glossary and suggested terms.
type ComponentTerms struct {
	File      string       `json:"file,omitempty"`
	Uses      []string     `json:"uses,omitempty"`
	Avoid     []string     `json:"avoid,omitempty"`
	Suggested []Suggestion `json:"suggested,omitempty"`
}

// Suggestion is a word that is frequent in one component and rare elsewhere.
type Suggestion struct {
	Term  string  `json:"term"`
	Score float64 `json:"score"`
}

// Usage counts where a component names a concept it does not own, by channel.
type Usage struct {
	Component string   `json:"component"`
	Concept   string   `json:"concept"`
	Idents    int      `json:"idents,omitempty"`
	Strings   int      `json:"strings,omitempty"`
	Comments  int      `json:"comments,omitempty"`
	Evidence  []string `json:"evidence,omitempty"`
}
