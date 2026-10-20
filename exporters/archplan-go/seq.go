package main

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// Scenario is one entry of a seqtrace steps.json file: a JSON array of
// recorded scenarios, each with a call tree reduced to a diagram.
type Scenario struct {
	Name    string   `json:"name"`
	Handler string   `json:"handler"`
	Base    string   `json:"base"`
	Guards  string   `json:"guards,omitempty"` // "off" when the run had its guards switched off
	Diagram *Diagram `json:"diagram"`
}

// Diagram is a recorded call tree: the participants and the root call.
type Diagram struct {
	Participants []string `json:"participants"`
	Root         Step     `json:"root"`
}

// Step is one call, or a loop when Repeat is set. A loop has no endpoints;
// its Children are one iteration. Args are "name=value" strings. Outcome
// marks a refusing return (error, false, status:<4xx>). A call to another
// process has Via "http" and a Route; its Children can hold the server's
// call tree. Span is the id that correlates a client call with the server
// trace it caused.
type Step struct {
	From     string     `json:"from,omitempty"`
	To       string     `json:"to,omitempty"`
	Fn       string     `json:"fn,omitempty"`
	Via      string     `json:"via,omitempty"` // set for a call on another goroutine, or "http"
	Outcome  string     `json:"outcome,omitempty"`
	Route    *HTTPRoute `json:"route,omitempty"`
	Span     string     `json:"span,omitempty"`
	Args     []string   `json:"args,omitempty"`
	Repeat   int        `json:"repeat,omitempty"`
	Children []Step     `json:"children,omitempty"`
}

// IsLoop reports whether s is a loop rather than a call.
func (s *Step) IsLoop() bool { return s.Repeat > 0 }

// ParseSteps reads a seqtrace steps.json.
func ParseSteps(data []byte) ([]Scenario, error) {
	var out []Scenario
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse steps.json: %w", err)
	}
	return out, nil
}

// seqBuilder flattens a seqtrace call tree into messages.
type seqBuilder struct {
	seq    Sequence
	partOf func(string) string
	seen   map[string]bool
	count  map[string]int
	nloop  int
}

// SequenceFrom converts one seqtrace scenario. partOf maps a seqtrace
// participant (usually a module-relative package path) to a model participant;
// participants it maps to "" keep their own name. Labels keep argument names
// and drop their values, which can be secrets.
func SequenceFrom(sc Scenario, partOf func(string) string) Sequence {
	b := &seqBuilder{
		seq:    Sequence{Title: sc.Name, Entry: sc.Handler, Source: "seqtrace", Guards: sc.Guards, Fragments: map[string]Fragment{}},
		partOf: partOf, seen: map[string]bool{}, count: map[string]int{},
	}
	b.participant(sc.Diagram.Root.From)
	for _, p := range sc.Diagram.Participants {
		b.participant(p)
	}
	b.walk(sc.Diagram.Root, 0, "")
	if len(b.seq.Fragments) == 0 {
		b.seq.Fragments = nil
	}
	return b.seq
}

func (b *seqBuilder) participant(p string) string {
	id := b.partOf(p)
	if id == "" {
		id = p
	}
	if !b.seen[id] {
		b.seen[id] = true
		b.seq.Participants = append(b.seq.Participants, id)
	}
	return id
}

func (b *seqBuilder) walk(s Step, depth int, frag string) {
	if s.IsLoop() {
		b.nloop++
		id := "L" + strconv.Itoa(b.nloop)
		var parent *string
		if frag != "" {
			parent = &frag
		}
		b.seq.Fragments[id] = Fragment{Kind: "loop", Label: "×" + strconv.Itoa(s.Repeat), Parent: parent}
		for _, c := range s.Children {
			b.walk(c, depth, id)
		}
		return
	}
	from, to := b.participant(s.From), b.participant(s.To)
	key := from + ">" + to + ":" + s.Fn
	b.count[key]++
	b.seq.Messages = append(b.seq.Messages, Message{
		ID: key + "#" + strconv.Itoa(b.count[key]), From: from, To: to,
		Label: s.Fn + "(" + argNames(s.Args) + ")", Depth: depth, Fragment: frag,
		Async: s.Via != "", Via: s.Via, Outcome: s.Outcome, Route: s.Route, Span: s.Span,
	})
	for _, c := range s.Children {
		b.walk(c, depth+1, frag)
	}
}

// argNames keeps the names of "name=value" arguments.
func argNames(args []string) string {
	names := make([]string, 0, len(args))
	for _, a := range args {
		if name, _, ok := strings.Cut(a, "="); ok {
			names = append(names, name)
		}
	}
	return strings.Join(names, ", ")
}

// SequenceID turns a scenario name into a stable sequence ID.
func SequenceID(name string) string { return slug(name) }
