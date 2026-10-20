package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Generator names this exporter in the model's source section.
const Generator = "archplan-go"

// Options configures Export.
type Options struct {
	Root       string   // module root
	ArchLint   string   // go-arch-lint config, relative to Root or absolute; "" for none
	Tags       string   // build tags for go list
	Patterns   []string // packages to describe
	Steps      []string // seqtrace steps.json files
	Exclude    []string // module-relative directories to skip
	Depth      int      // group packages without a component by this many path segments
	Unexported bool
	NoVocab    bool // skip the vocabulary section
	Warn       func(format string, args ...any)
}

// Export builds the archmodel for the module at opt.Root. Without a
// go-arch-lint config, every package (or group of packages, see
// Options.Depth) is a component and the policy allows everything.
func Export(ctx context.Context, opt Options) (*Model, error) {
	if opt.Warn == nil {
		opt.Warn = func(string, ...any) {}
	}
	al := &ArchLint{Components: map[string]Component{}, Policy: Policy{Default: Allow, Rules: []Rule{}}}
	layer := ""
	if opt.ArchLint != "" {
		file := opt.ArchLint
		if !filepath.IsAbs(file) {
			file = filepath.Join(opt.Root, file)
		}
		src, err := os.ReadFile(file)
		if err != nil {
			return nil, fmt.Errorf("read go-arch-lint config: %w", err)
		}
		if al, err = ParseArchLint(src, configName(opt.Root, file)); err != nil {
			return nil, err
		}
		if al.Policy.Rules == nil {
			al.Policy.Rules = []Rule{}
		}
		layer = UnmappedLayer
	}
	module, err := goModule(opt.Root)
	if err != nil {
		return nil, err
	}
	list, err := ListPackages(ctx, opt.Root, opt.Tags, opt.Patterns)
	if err != nil {
		return nil, err
	}
	for _, p := range list {
		if p.Error != nil && !p.DepOnly {
			opt.Warn("%s: %s", p.ImportPath, p.Error.Err)
		}
	}
	gr, err := ExtractGo(list, al.Components, GoOptions{
		Root: opt.Root, Module: module, Exclude: append(al.Exclude, opt.Exclude...), Depth: opt.Depth,
		Layer: layer, Unexported: opt.Unexported, Vocab: !opt.NoVocab,
	})
	if err != nil {
		return nil, err
	}
	if opt.ArchLint != "" && len(gr.Unmapped) > 0 {
		opt.Warn("%d packages are not in %s; components were added for them: %s",
			len(gr.Unmapped), opt.ArchLint, strings.Join(gr.Unmapped, ", "))
		al.Layers = append(al.Layers, Layer{ID: UnmappedLayer, Name: "Not in " + filepath.Base(opt.ArchLint)})
	}
	for _, w := range gr.Warnings {
		opt.Warn("%s", w)
	}
	if gr.TypeErrors > 0 {
		opt.Warn("%d type errors; the affected types may be incomplete", gr.TypeErrors)
	}
	m := &Model{
		Format:       Format,
		Source:       source(ctx, opt.Root, module, opt.ArchLint != ""),
		Layers:       al.Layers,
		Components:   al.Components,
		Dependencies: gr.Dependencies,
		Policy:       &al.Policy,
		Types:        gr.Types,
		Vocabulary:   gr.Vocabulary,
		Files:        gr.Files,
		Signatures:   gr.Signatures,
		Literals:     gr.Literals,
		Routes:       gr.Routes,
	}
	if m.Dependencies == nil {
		m.Dependencies = []Dependency{}
	}
	if err := addSequences(m, opt.Steps); err != nil {
		return nil, err
	}
	m.Source.Capabilities = capabilities(m)
	return m, nil
}

// capabilities lists what the model contains, in the terms of
// exporter-plugins.md.
func capabilities(m *Model) []string {
	var out []string
	add := func(name string, ok bool) {
		if ok {
			out = append(out, name)
		}
	}
	internal, fields := false, false
	for _, c := range m.Components {
		internal = internal || c.Kind != KindExternal
	}
	for _, t := range m.Types {
		fields = fields || len(t.Fields) > 0
	}
	add("components", internal)
	add("deps", len(m.Dependencies) > 0)
	add("files", len(m.Files) > 0)
	add("types", len(m.Types) > 0)
	add("fields", fields)
	add("calls", len(m.Sequences) > 0)
	add("signatures", len(m.Signatures) > 0)
	add("literals", len(m.Literals) > 0)
	return out
}

// configName is the config path as the policy enforcer reports it: relative
// to the root when the file is inside it.
func configName(root, file string) string {
	if rel, err := filepath.Rel(root, file); err == nil && !strings.HasPrefix(rel, "..") {
		return filepath.ToSlash(rel)
	}
	return file
}

// addSequences adds the scenarios of seqtrace steps.json files. A
// participant is a package path; it maps to the component holding that
// package, tried as an import path, a module-relative path, and a path
// below internal/.
func addSequences(m *Model, steps []string) error {
	ix := newComponentIndex(m.Source.Module, m.Components)
	partOf := func(p string) string {
		for _, path := range []string{p, m.Source.Module + "/" + p, m.Source.Module + "/internal/" + p} {
			if id := ix.of(path); id != "" {
				return id
			}
		}
		return ""
	}
	for _, f := range steps {
		data, err := os.ReadFile(f)
		if err != nil {
			return fmt.Errorf("read steps: %w", err)
		}
		scs, err := ParseSteps(data)
		if err != nil {
			return fmt.Errorf("%s: %w", f, err)
		}
		if m.Sequences == nil {
			m.Sequences = map[string]Sequence{}
		}
		for _, sc := range scs {
			if sc.Diagram != nil {
				m.Sequences[SequenceID(sc.Name)] = SequenceFrom(sc, partOf)
			}
		}
	}
	return nil
}

// goModule reads the module path from root/go.mod.
func goModule(root string) (string, error) {
	data, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		return "", fmt.Errorf("%s is not a module root: %w", root, err)
	}
	sc := bufio.NewScanner(bytes.NewReader(data))
	for sc.Scan() {
		line, _, _ := strings.Cut(sc.Text(), "//")
		f := strings.Fields(line)
		if len(f) == 2 && f[0] == "module" {
			if p, err := strconv.Unquote(f[1]); err == nil {
				return p, nil
			}
			return f[1], nil
		}
	}
	return "", errors.New("go.mod has no module line")
}

var remoteRepo = regexp.MustCompile(`[:/]([^/:]+/[^/]+?)(\.git)?$`)

func source(ctx context.Context, root, module string, archLint bool) Source {
	s := Source{
		Languages:   []string{"go"},
		Module:      module,
		Generator:   Generator,
		GeneratedAt: time.Now().UTC().Format(time.RFC3339),
		Commands: map[string]string{
			"build": "go build ./...",
			"test":  "go test ./{path}/...",
		},
	}
	if archLint {
		s.Commands["rules"] = "go-arch-lint check"
	}
	if url, err := run(ctx, root, "git", "remote", "get-url", "origin"); err == nil {
		if m := remoteRepo.FindStringSubmatch(url); m != nil {
			s.Repo = strings.ToLower(m[1])
		}
	}
	s.Ref, _ = run(ctx, root, "git", "rev-parse", "--short", "HEAD")
	if st, err := run(ctx, root, "git", "status", "--porcelain", "--untracked-files=no"); err == nil {
		s.Dirty = st != ""
	}
	return s
}

func run(ctx context.Context, dir, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	// Keep git status from refreshing the index: the export only reads.
	cmd.Env = append(os.Environ(), "GIT_OPTIONAL_LOCKS=0")
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return strings.TrimSpace(string(out)), nil
}
