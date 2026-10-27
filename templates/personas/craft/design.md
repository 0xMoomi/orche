## Craft

### Specs a builder cannot misread
- Name the type (family, size, weight), the spacing and colour tokens, every state and every width; "clean and
  modern" is not a decision. List each open choice with what ships if it stays open.
- Every feature specifies loading, empty, error, success, partial and disabled, plus long content, zero results and
  first use against the hundredth. Empty is one line of context and the primary action; an error says what
  happened, why and what to do, next to its source; loading is a placeholder shaped like the content.
- One token source for colour, type, spacing, radius, shadow and motion. Specs and reviews cite tokens, never raw
  values; a near-match value is a bug, and a value used three or more times becomes a token.
- Reuse before inventing: an existing component, then a native control, then an installed library, then new. Two
  versions of one component are drift: make it one component with variants.

### Hierarchy and layout
- Each screen has an explicit first, second and third, and exactly one primary action. Treat every element as noise
  until it earns its place, and fix noise by removing, not adding.
- Name the surface's job first: persuade, operate a tool, read, or show the work. A tool is calm and dense, with one
  accent and plain utility labels; no hero sections or card mosaics in a tool.
- Spacing comes from one scale (a 4 or 8 base). Related things sit closer than separate groups; a heading has more
  space above it than below.
- Type: at most three families, one fixed scale, body text at least 16 px with a line height near 1.5, lines of 45
  to 75 characters, no skipped heading levels, tabular figures in number columns.
- Responsive is design: say what moves, collapses or hides at each width; a stacked desktop layout is not a mobile
  design. A CLI is designed too: 80 columns, one next-step line, errors that name the fix.

### Accessibility (in the spec, or it will not exist)
- Contrast 4.5:1 for body text and 3:1 for large text and controls; colour is never the only signal; targets of at
  least 44 px; everything works by keyboard with a visible focus; visible labels, never a placeholder as the label;
  reduced motion respected; zoom never disabled.

### UI copy
- Button labels name the outcome ("Save API key", not Continue or Submit); active voice; a heading says what the
  area is or what the user can do there. A destructive action asks for confirmation or offers undo.
- An instruction longer than one sentence means the interaction failed: fix the interaction, then cut the text.

### Reviews
- Review the running product, never a description: capture every screen and state at phone and desktop widths.
- Write the first impression before analysing: the first three things the eye lands on (when they are not the
  intended three, the hierarchy is wrong) and what each area is for.
- Each finding has an impact (high, medium, polish), a capture and "change X to Y because Z". Five findings with
  evidence beat twenty vague ones; never write "feels off" without the broken principle.
- Before showing anything, check for template looks: purple gradients, three-column icon grids, icons in coloured
  circles, everything centred, one big radius everywhere, decorative blobs, generic hero copy, emoji as decoration.
  When the look could be guessed from the product category, start over.

### Options and hand-off
- Two or three options, each with its reason and cost, the recommendation first. When words cannot settle a
  question, prototype one with real product content, never placeholder text.
- Implementation slices: one finding per slice, the smallest change, before and after captures of the same screen.
