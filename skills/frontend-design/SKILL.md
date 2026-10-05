---
name: frontend-design
title: Frontend Design
description: Build production-grade interfaces with a distinct visual point of view instead of generic AI-default styling.
when_to_use: building a page, UI, dashboard, landing page, component, or styling anything visual
triggers: landing page, dashboard, website, webpage, frontend, redesign, styling, ui/ux, user interface, html, css, home page, hero section, mockup, prototype
version: 1.0.0
---

# Frontend Design

Your job is to produce interfaces that look like a designer made a decision, not
like a framework rendered a default. Most AI frontends fail the same way: purple
gradient on a dark background, everything centered, three identical feature
cards, Inter at 16px, rounded-xl on every box. That is the look of nobody.

## Pick a direction before writing code

Decide the following, in one line each, and then commit to them for the whole
file. Do not mix two directions.

1. **The reference.** Name a real thing this should feel like: a Swiss transit
   timetable, a lab instrument panel, a 1970s print catalogue, a Bloomberg
   terminal, a Mid-Century poster, a Japanese convenience store receipt.
2. **The type pairing.** One display face with character for headings, one
   neutral face for body. Two faces, not four. Reach for system stacks and
   Google Fonts; set real sizes (a display scale like 12/14/16/20/28/44/72), not
   five sizes that are all roughly 16px.
3. **The colour logic.** Choose a background that is not pure white or pure
   black. Pick one accent and use it as punctuation - under 10% of the pixels.
   Do not invent a 6-colour palette; derive tints and shades from two hues.
4. **The structural idea.** What is the one memorable layout move? A left rail of
   oversized numerals, a rules-and-columns grid, a hard split, an editorial
   hang. Every good page has exactly one.

## Rules that separate a real interface from generated filler

- **Spacing is a system.** Pick a base unit (4px or 8px) and only use multiples.
  Inconsistent padding is the single most common tell.
- **Hierarchy through size, weight and space - in that order.** Not through
  colour. If everything is bold, nothing is.
- **Alignment must be real.** Left-align body text to one edge across the whole
  page. Centered paragraphs over three lines read as amateur.
- **Borders and shadows are alternatives, not companions.** Flat design uses
  hairlines and space; layered design uses shadow. Not both on the same card.
- **Contrast is not optional.** Body text needs 4.5:1 against its background.
  Muted grey on off-white at 14px is unreadable, not tasteful.
- **Empty states, hover, focus and disabled are part of the design.** A button
  with a focus ring you never defined is a bug, not an omission.
- **Motion is fast and functional.** 120-200ms, `ease-out`, and it must convey
  something (arrival, state change). No bounce, no parallax, no fade-in on
  scroll for its own sake.

## Banned unless the brief explicitly asks for it

Purple-to-blue gradients. Glassmorphism. Emoji as iconography. The three-card
feature row. A hero with centred text over a stock-photo blur. `border-radius`
above 16px on everything. Drop shadows with no offset direction.

## Output

Deliver a **single self-contained HTML file** with its CSS in one `<style>` block
and any JS in one `<script>` block. Real content, not `Lorem ipsum` and not
"Feature One / Feature Two". If it is a dashboard, populate believable data. If
it is a form, include validation states. It must open and look finished with no
build step and no network beyond fonts.

Then state, in two lines, the reference you chose and the structural idea - so
the next change can stay inside the same direction instead of drifting.
