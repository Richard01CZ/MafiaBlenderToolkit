# Mafia Toolkit — 1.0.0

Blender addon for Mafia's LS3D formats: `.4ds` models, `.5ds` animations,
`.tck` movement tracks and `.6ds` shadows. This began as a refactor of the
single-file `4ds.py` (0.6.2), and has since grown to cover all four formats
with a set of data-loss bugs fixed along the way.

## Installing

Install `mafia-toolkit-1.0.0.zip` through
*Edit → Preferences → Add-ons → Install from Disk*, then set the **Texture
Folder** in the addon preferences to the game's `maps` directory so imports can
find textures.

**Or from the command prompt, with nothing but Blender on the machine.** The
zip and `mbt.bat` beside it are a whole working copy — nothing unpacked,
nothing installed yet:

```bat
mbt.bat --install --textures "D:\Hry\Mafia Editovani\maps" --open "D:\models\tommy.4ds"
```

One command: it finds the zip next to it, installs it into the newest Blender
it finds, switches it on, keeps the texture folder, saves the preferences — so
the addon is there next time Blender opens — and then opens the model, all in
one Blender. `--install` takes a zip by name as well, and with no name it
installs whatever copy the command itself came out of: the zip beside it, or
the folder it sits in.

It also runs **straight out of the zip without installing anything** — leave
`--install` off and the same command opens, checks and exports, from a copy
unpacked where temporary files go. Useful for trying a build, or for a machine
you would rather not change.

`MAFIA_BLENDER` set to a `blender.exe` picks the Blender rather than taking the
newest. No Python is needed anywhere in this: Blender brings its own.

To build and install in one step instead — this one is for the workshop, and
does need Python:

```bash
build-dev.bat
```

That writes `mafia-toolkit-1.0.0-dev.zip` beside the package and installs it
into the newest Blender it finds — worth using rather than zipping by hand,
because a Blender upgrade starts a fresh config folder and leaves the addon
behind in the old one. The Texture Folder does not carry across a Blender
upgrade either, so set it again after one.

**Two builds come out of the same tree.** The workshop build, the one above,
holds everything — the proving suite, the corpus verifiers, the build script —
so the Blender it lands in can run the tests against exactly what is installed.
The build that ships holds the addon and nothing else:

```bash
build-release.bat
```

That writes `mafia-toolkit-1.0.0.zip` and installs nothing, so the Blender you
work in keeps the workshop build. Either `.bat` passes anything typed after it
straight to the build script, where `--no-install` and `--blender PATH` still
work; the script itself is `io_mafia_toolkit/tools/build_addon.py`.

Requires Blender 5.2 or newer.

## The guides

* **`LS3D-4DS-User-Guide.html`** — the way in, in the order you meet it:
  installing, a tour, a first model, the checks, a prop with LODs, materials, a
  car, a character, animation and the rest.
* **`LS3D-4DS-Field-Manual.html`** — the reference: every frame type, every
  panel of switches, every check and what the format holds.
* **`LS3D-4DS-Dummy-Names.html`**, **`LS3D-4DS-User-Properties.html`**,
  **`LS3D-4DS-Flag-Usage.html`**, **`LS3D-4DS-Joint-Viewer.html`** — the names
  the game looks for, the text a frame can carry, which flags are really used,
  and a browser view of a model's joints.

This file is neither: it is the record of what changed and why.

## The MBT CLI

The same addon, driven from a command prompt — for converting a folder of
files, for putting it in a script, for handing a model to Blender from another
program, or for looking at one model quickly:

```bat
mbt.bat --textures "D:\Hry\Mafia Editovani\maps" --open "D:\models\Tommy.4ds" --check
```

It finds Blender itself and runs the addon inside it, so nothing but Blender is
needed. One file serves everywhere: it looks for the addon's code beside
itself, in an `io_mafia_toolkit` folder under it, or in a `mafia-toolkit*.zip`
next to it — which is why the copy inside the package and the copy at the top
of the project are the same file, and a test holds them that way. Paths can be
full or relative to where you are standing —
they are made absolute before Blender is started, because Blender is a program
of its own and what counts as "here" is not its business.

`--install` puts the addon into Blender, as above. `--open` takes a `.4ds`,
`.5ds`, `.tck` or `.6ds` and reads the kind from the name; repeat it to load
an animation onto its model. `--export` writes the scene back out, `--save`
keeps it as a `.blend`, `--check` and `--check-animation` run the export checks
without writing anything, and `--gui` leaves Blender open with the result
instead of closing it. `--option name=value` passes a setting through to the
import or export dialog exactly as it is named there — `selection_only`,
`write_animation`, `own_collection` and the rest — and a name no dialog has is
refused rather than ignored.

The exit code is 0 when everything asked for was done, 1 when a step was
refused and 2 when the command itself made no sense, so a batch script can
tell, and 3 when no Blender could be found. `mbt.bat --help` lists the lot.
Without the `.bat` it is `python io_mafia_toolkit/cli.py …` — that path does
need Python, and it finds Blender and starts itself again inside it — and from
inside Blender `blender --background --python io_mafia_toolkit/cli.py -- …`.

## Layout

Each format owns a folder, with what they share underneath. The folders are
named after the formats, so `4ds`, `5ds` and `6ds` start with a digit and
Python's `import` statement cannot spell them — `packages.py` reaches them
through `importlib` instead. Inside each, ordinary relative imports work.

| Folder | Holds |
| --- | --- |
| `common/` | `binary.py` reader/writer, `constants.py`, `convert.py` axis conversion, `report.py` |
| `4ds/codec/` | Pure-Python codec: bytes ↔ dataclasses. **No `bpy` import** |
| `4ds/` | `importer.py` and `import_*.py`, `exporter.py` and `export_*.py`, `mesh.py`, `materials.py`, `validation.py`, `viewport.py`, `ops_*.py`, `ui.py` |
| `5ds/` | `codec.py`, `io.py` (curves and actions), `ops.py`, `ui.py` (the sidebar) |
| `tck/` | `codec.py`, `io.py` (the movement empty), `ops.py` |
| `6ds/` | `codec.py`, `io.py` (shadow pieces as meshes), `ops.py` |
| `packages.py` | Reaching the format packages, whose names start with a digit |
| `properties.py` | `bpy` property registration |
| `tools/` | Standalone corpus verifiers and the Blender test suite |

The codec layers deliberately have no Blender dependency. They convert between
raw bytes and plain dataclasses, which means they can be run against a
directory of real game files without launching Blender — and they are: see
below.

## Testing

**Codec layer**, no Blender needed. Parses every file under a directory and
checks that re-serializing reproduces the original bytes exactly:

```bash
python io_mafia_toolkit/tools/verify_corpus.py "D:/Hry/Mafia Editovani/models"
python io_mafia_toolkit/tools/verify_animations.py "D:/Hry/Mafia Editovani"
python io_mafia_toolkit/tools/verify_tracks.py "D:/Hry/Mafia Editovani"
python io_mafia_toolkit/tools/verify_shadows.py "D:/Hry/Mafia Editovani"
```

Current results against the shipping game data:

| Format | Byte-exact | Not read |
| --- | --- | --- |
| `.4ds` models | **3116 of 3118** | 2, being format versions 27 and 58 |
| `.5ds` animations | **2726 of 2734** | 8, being versions 4 and 11 |
| `.tck` tracks | **283 of 293** | 10: five at version 2, five with a nonsense version |
| `.6ds` shadows | **37 of 37** | none |

Everything not read fails with a clear message naming the version, rather than
silently producing garbage.

**End-to-end**, inside Blender. Imports each model, exports it, compares the two
documents structurally, then runs one regression check per fixed bug. Use the
driver rather than calling Blender directly — it gives each model its own
process, because repeatedly importing into one `--background` session corrupts
Blender's memory after a while no matter how the scene is cleared in between:

```bash
python io_mafia_toolkit/tools/run_blender_tests.py --blender "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" --models "D:/Hry/Mafia Editovani/models" --maps "D:/Hry/Mafia Editovani/maps" --regression-dir "D:/Hry/Mafia Editovani/models" --limit 50
```

`--models` accepts a folder, a single `.4ds` path, or a text file listing paths.
Current results:

* 50-model sample plus a set covering sectors, portals, mirrors, billboards,
  instances, animations, event cues, movement tracks, shadows and material
  previews: **286 checks, 0 failures** (236 regression checks and 50 model
  round-trips), from the command above. Several of the checks re-export
  thirteen models chosen for their odd frames, two characters and two morphs,
  and compare every value bit for bit; another exports six more twice over and
  checks the second round changes nothing.
  The driver counts a run that stops partway as a failure: an error inside one
  check ends the script while Blender still exits cleanly, so the checks after
  it would otherwise go missing without a word.
* Every model in the game, imported and exported once with nothing changed and
  compared bit for bit: **2801 of 3119 come back exactly**, the save date aside.
  Two more are format versions the add-on does not read. Of the other 318:
  - 284 models with joints differ where their joints round, in what is
    worked out from the joints, and in the last digits of their influence
    boxes; 130 of them also have face morphs whose target normals differ, and
    121 list a morph region's vertices out of order;
  - 26 more morphs differ only in their target normals;
  - `!TommyHIGH2`, `!TommyHIGH3`, `!TommyHIGH4` and `Untitled`, from an older
    tool, differ in their joints and a few worked-out values;
  - `civil11` and `civil11M` differ in some bone group boxes, and `civil11M`
    also in its morphs;
  - `4old071` loses one vertex, and `prejimka` differs in some portal planes
    and sector boxes.
* The same models exported, imported and exported again: **all 3119 come out
  the same the second time**. Nothing drifts, however often a model goes round.

  What each of those is, and why Blender cannot hold it, is under
  *Re-exporting writes back the values a model came with*.
* Every mission scene (80 readable, up to 6.5 MB and 2053 frames each), the same
  way: **4 come back exactly**, the save date aside. In the other 76 nothing
  differs but worked-out values: portal planes, sector boxes in 9 of them, and
  FREERIDE's five empty LODs.

`blender_tests.py` can still be run directly against a handful of models if you
prefer, but expect the session to become unstable past roughly ten.

## What changed

Blender shows it as **1.0.0**, with no warning beside the name, and asks for
Blender 5.2 or newer - the version it has been proven on.

### Importing through the command line, with a window open

A script handed to a Blender that has a window runs **outside** that window's
own context: `bpy.context.window` is empty although a window is open, and an
operator that polls for what is being looked at refuses. Placing a character's
bones switches the armature into Edit Mode, which is one of those, so importing
a model through `mbt` came out **broken with a window open** — and perfect
without one, which is why every test in the suite passed: the suite runs in the
background, where there is no window to be outside of.

The command now borrows the window when there is one, and the whole run — the
import, the checks, the export — happens inside it. A windowed import is again
identical to one from **File ▸ Import**, object for object, material for
material, texture for texture. This is what an editor driving the add-on
through `mbt --open … --gui` was hitting.

### The morph panel makes its own shape keys, and settles its own regions

**Each button does one thing.** The **+** makes a new shape key and adds it -
the group's basis first, then its targets, named the way an import names them
(`G1_Basis`, `G1_Target1`, and so on), built from the mesh as it stands. It
used to open a picker of the keys already on the mesh instead, which found
nothing on a fresh mesh and said so; that was the whole way in, so a morph
could not be started at all. Taking a key the mesh already carries is its own
intention and now has its own button beside **+**, which opens that picker.

**Removing a target removes the morph.** The **−** takes the shape key off the
mesh along with the target; it used to unlink the target and leave the key
behind, so emptying a group left a pile of loose keys that nothing played -
and the next **+** offered them straight back. A key another morph group still
holds is kept, since deleting it would empty a group that is in use, and the
message says which. Removing a group's basis warns that the next target is its
basis now.

**A group's region is no longer a question.** The panel asked which vertex
group held it. A group that came out of a file keeps the region the file
listed - those vertices whether they move or not - and a group made here
covers the vertices its own targets move, which is what the export works out
when no group is named. The field is gone; the panel says which of the two it
is, and warns when a named region's vertex group has been deleted since.
Picking a group in the list brings its region up as the mesh's active vertex
group, so Blender's own vertex group tools are pointed at the right one.

### The panels read at the width they are drawn at, and the handles read apart

Four things came out of looking at the add-on in a real window rather than
reading its code:

- **The turn rings were a heavy band** lying across the box, nineteen pixels
  wide, once the rings were held at a fixed size on screen. They are a thin
  nine-pixel band now - still easy to hit, and you can see the box through
  them.
- **The handles that slide a box are arrows now**, pointing along the axis
  they slide. They used to be grab boxes, the same shape as the six that
  resize the box and sitting a few dozen pixels away from them, so the only
  thing telling the two kinds apart was their color.
- **Panel notes are no longer cut short.** Blender draws a label as one line
  and cuts off whatever does not fit, with no way to read the rest: measured
  against the sidebar at the width Blender opens it, 42 lines did not fit. A
  note is now written as one sentence and broken to the width the panel is
  actually being drawn at, so nothing is lost and the text re-flows when you
  drag the sidebar wider or narrower.
- **Influence Boxes and In Front have a row each** in the 4DS Model tab.
  Side by side, the first was cut short.

### Every box shows its middle, and a joint's box is linked to its joint

A box drawn as twelve edges says nothing about where its middle is, and the
middle is a number the panel edits. Every box the add-on outlines - a dummy's, a
mirror's bound, a mirror's view box and each joint's influence box - now carries
a **dot at its middle**, in the box's own color, so it can be placed by eye and
not only by typing.

A joint's box can sit anywhere on the model, the same way a dummy's box can sit
off its empty, so it is marked the same way: a box that does not sit on its
joint has a **dashed line** from its middle back to the joint it belongs to.
On a weighted character, where a dozen green boxes overlap, that line says which
joint each box is weighting. It follows the viewport's **Relationship Lines**
overlay toggle, exactly as the dummy's line does, and a box sitting on its joint
gets no line because there is nothing to point at.

### The box handles keep their distance, and a drag can be taken back

Two things made the joint box's handles hard to use. A joint's box is often a
few centimeters across, and every handle was placed on the box itself: six
faces, three arrows and three rings inside a clump a few pixels wide, where a
click was a guess. And nothing a handle did reached the undo stack, so Ctrl+Z
after a drag stepped over it to whatever was done before.

- **Each kind of handle keeps its own ring on screen.** The faces sit 44 pixels
  out from the middle of the box, the arrows 80, the rings 116 - measured on
  screen, so an axis leaning away from the view is pushed out as far as it
  needs rather than as far as the others. On a 2 cm box the closest two handles
  were 2.4 pixels apart and are now 35. A box already big enough on screen is
  left exactly where it is: its handles stay on its faces, as before.
- **A handle standing end-on to the camera is taken away** until the view
  moves. It used to draw over the middle of the box, on top of everything else,
  and a drag along it says nothing about a distance anyway. The same goes for a
  ring seen edge-on, which is a line through the rest.
- **One kind at a time, if you like.** The 4DS Model tab has **Handles**: All,
  Resize, Move or Turn. On a crowded skeleton, picking one kind leaves nothing
  else to hit by mistake.
- **Every drag is a step of its own.** Letting go of a face, an arrow, a ring,
  a dummy's box or a light's aim ball leaves one step for Ctrl+Z to take back,
  and a drag let go where it started leaves none. A cancelled drag puts the box
  back as before, as it always did.

### Set Default Mesh Origin finds the place from the character itself

It used to stand every armature at one place: Tommy's hip spot, 1.08 m up and
3.6 cm forward, measured from the world's center - however far the character
stood from it. The default place now comes from the character's own skeleton:
6.66 cm above the point midway between its `l_thigh` and `r_thigh` joints and
3.9 mm forward, which is where Tommy's origin stands from his. So it follows
the character wherever it stands, moved or not, and whatever its size.

A character of another size stands at its own hip height, not Tommy's, and the
game's own animations - which put the origin where Tommy's is - lift or lower
it by the difference; the button says how much when it is more than 10 cm, and
so does the export's warning, which then points at the character's size rather
than at the button.

The joints count where they are posed, not where they rest: a pose nobody
keyed is the model, and the export writes the skeleton bent that way, so the
origin belongs with it. An animation posing them is another matter - what
shows is the frame, not the model - and the button says to unload it first.

An origin already standing at that place is left exactly where it is: within
10 cm of it, and unturned. Every character the game ships stands
there - each placed by hand, up to 5.2 cm from where the joints alone would
put it, 6.6 cm on the cow - so pressing it on any of the 279 changes nothing,
not a bit of the origin, its scale or a bone. An armature moved as an object
keeps where the move put it, taken into where it stands. A skeleton with no
hip joints has nothing to find the place from, and the button says so.

### Weights from the boxes: a skirt splits between the legs, hair follows the head

`snowWh`, weighted from its boxes, came out with the skirt lopsided - the right
shin held most of the hem on both sides - and the hair's lower edge, at the
nape and beside the jaw, following the chest. Two rules did it, and a third
made new boxes worse:

- **Joints of different chains now meet where the mesh is nearer the one.** A
  region grows along the mesh's edges, a ring at a time, which is the game's
  own rule. Counted in edges, a region runs ahead wherever the triangles lean
  its way, and across a skirt the one leg's region reached the other side
  first. Between a joint and the joints above or below it the ring count
  still decides, as the game has it; where the regions of joints that are no
  kin of each other meet - a left leg and a right, a leg and the spine - the
  one nearer along the surface takes the vertex. Where their boxes overlap,
  the box whose middle the vertex is nearer takes it, where the larger share
  used to.
- **A piece nothing reaches follows one joint, whole.** Hair is modeled on its
  own, no box holds it, and each vertex used to copy the nearest weighted
  vertex - the scalp for most, the chest for the ends at the nape, so it tore
  when the head turned. It now follows the joint it rests on most: of its
  vertices within a centimeter of its closest approach to the body, the joint
  the most of them are nearest.
- **A fitted box measures only its own joint's side.** Fitting sized a box to
  all the mesh around the joint, and a skirt round both legs is as wide as
  both, so each leg's box reached right across to the other leg. It now counts
  only what is nearer its joint than any joint outside its own chain.

Measured on the game's 289 skinned models with nothing painted, the weights
come closer to the game's own: 105,638 of 109,578 vertex positions (96.4%)
against 105,570, 69 models exactly against 68, 21 models closer and 5 a
vertex or three further. The game's women in dresses gain the most -
`Czena09`, `Czena10` and `Czena11` from 216 of 225 to 224. Tommy stays at
477 of 480. `Pomni` with every box fitted leaves 55 vertices following
nothing but the hips, against 86. Two tests hold it: a skirt round two legs,
cut into triangles that all lean one way, splits down the middle with every
fitted box stopping at the other leg; a strip hanging from one joint down past
the next follows the first whole.

**Influence boxes can be drawn in front.** The **4DS Model** sidebar has an
**In Front** switch beside **Influence Boxes**: the boxes are drawn over
everything, so one buried in the mesh can be seen whole. Only the drawing
changes.

### A color key cuts all of its own pixels

`znpompeii`, the Bar Pompeii sign, drew solid in Blender: the brown between
its bulbs is the texture's key color, and not a pixel of it was cut. A pixel
counts as the key when each channel is nearer the key's byte than any other
byte, which the preview checks on the linear values Blender hands the node
graph. How far each channel could sit from the key was worked out as if the
linear key were a byte value, which made the window a third or less of what
it should be in the middle tones - and Blender's viewport does not turn a
texture's bytes into exactly the linear values they stand for. The sign's key,
(118, 100, 85), came back 0.0005 off on green against an allowance of 0.0004,
so every one of its pixels missed. Measured over all 256 byte values, the
viewport is off by up to 86% of the way to the halfway point between two bytes,
near white; Cycles is exact. Five byte values fell outside the old window, and
19 of the game's key textures have one of them in their key - 46 models, among
them `0vila`, `9mrak1`, `9vlak` and `%bank1`, drew those solid too.

The window now reaches halfway to the byte either side, worked out from the
byte the key was read from: the whole of what can be the key's, and not a step
past it. A test renders keys at a middle tone, near white and at black, cut
out and blended, and checks that the key's own pixel is cut and a pixel one
byte off on any channel is not.

### No bone for the mesh frame: the armature stands on it

A skinned mesh's frame - `base` on a character - is where the file keeps the
mesh's vertices, what the top joints of the skeleton hang from, and what the
game's character animations move and turn the body by. The game also uses its
place for the body's collision, the hit zones and where a body falls. It used
to be in Blender twice: as a bone called after the mesh, and as the mesh
object's origin, and the two had to be kept in the same place by hand. A mesh
whose origin was put anywhere else was refused, and moving the bone to the
mesh instead - which the preset's message offered - put the frame at the feet,
where every animation then lifted the body a metre into the air.

Now it is in Blender once, and it is the armature:

- **The armature stands where the mesh frame is.** It keeps that place in its
  **Delta Transform** - Blender's own offset beneath an object's location,
  rotation and scale - so its Location, Rotation and Scale stay at nothing,
  free for the body's movement. On a character its origin sits at hip height.
  The armature's 4DS panel shows where it stands, as **Mesh Frame**, and
  **Set Default Mesh Origin** in the 4DS Model sidebar tab stands an
  armature at the default mesh origin, found from the character's own
  skeleton, without moving a bone or the mesh. The joints are measured from it, as they are from the frame in the
  file. No bone stands for it.
- **The mesh's origin does not matter.** The export brings the vertices into
  the frame's terms wherever the mesh object sits; `Tommy` with his mesh's
  origin moved to one side writes the same model, each vertex within a
  rounding step. Left where the import puts it - on the armature, with
  nothing of its own - it is written to the bit.
- **The body turns about its own point.** A crouch or a roll keys the frame's
  place and turn; they land on the armature's own location and rotation,
  measured from where it stands, so the body turns about the hips the way the
  game turns it, between keys as well as on them. An armature turning about
  the feet would swing the hips 32 cm off halfway through a quarter roll keyed
  ten frames apart. The game's own crouch, rolls and `01ChuzeLajd` - which
  also scales the body - play back as the game plays them.
- **The mesh's own share is a vertex group.** A vertex shared between a top
  joint and the mesh frame keeps the mesh's share in the vertex group the
  Armature modifier names as its Vertex Group, with Invert on: Blender holds
  the vertex back by that much, exactly as the game does. The import sets it
  up under the mesh's name, and **Make Weights From Boxes** makes it where
  there is none. Measured against a bone doing the same job, the two place
  every vertex within 0.00000003 m.
- **Joints above or beside the mesh are an armature of their own.** Joints not
  below the skinned mesh stand at the model's origin in a second armature:
  `BobAut01`'s lone joint beside its skeleton, and `I04Delnik01+`'s `Bone01`,
  which its mesh hangs from - so its character armature hangs on `Bone01`, and
  turning `Bone01` carries the whole body as it does in the file. A file may
  hold more than one armature; it may still hold only one skinned mesh.
- **What it costs.** The bones are now measured from the hips rather than the
  feet, and a 32-bit bone reads back a joint's values a rounding step apart a
  little more often than before: over 42 of the game's characters, 935 joint
  values written a step off the file's against 721 before, and 7,186 skin
  values against 7,039 - the largest step 0.00000003 m. What a first export
  writes, every later one writes again unchanged.

### Re-exporting writes back the values a model came with

An imported model now exports with its values unchanged wherever Blender can
hold them. Nothing is kept aside from the import for the export to use:
everything is worked out again from the scene, the way the game's own files
work it out.

- **Transforms are written from each object's own location, rotation and
  scale**, to the bit, with the rotation's sign as set. They used to be taken
  apart from a matrix, which moved the last bits of nearly every rotation and
  turned some rotations' signs.
- **Vertices go out in the mesh's own order.** They used to be written in the
  order the triangles first used them, which renumbered nearly every mesh.
- **Frames go out in the order the scene holds them**, each as soon as the
  frame it hangs from has, with whatever hangs from a frame straight after it.
  Joints hanging from nothing go out where their armature sits. The import lays
  the scene out so that this gives back the file's order: all 290 of the game's
  models with joints come out in the order they shipped in.
- **A skin numbers its joints in the order of the mesh's vertex groups.** The
  game does not number them in the order of the skeleton (284 of its 290 models
  differ), so the import makes one vertex group per joint in the file's order,
  empty ones included.
- **Weights are written as set.** Only a weight of exactly 1 counts as
  unblended. A weight of 0.9999996 used to be rounded up to 1, which moved
  vertices from one part of the skin to another in 17 models, `civil05` among
  them.
- **A skinned mesh is read before the armature bends it**, so its vertices are
  not rounded on the way through.
- **Faces that name a vertex twice are kept.** Blender cannot hold such a face
  (entering Edit Mode on one freezes it), so the repeat gets a copy of the
  vertex, placed after every vertex the file has, and the face becomes an
  ordinary triangle with no area. The export writes the copy as the vertex it
  repeats. The 1203 such faces in the game's files used to be dropped.
- **Odd values are kept**: a position or UV that is not a number, and a normal
  of no length, go back out as they came. The game ships all three.
- **A face group with no material gets an empty material slot** of its own,
  rather than slot 0 and whatever material landed there.
- **A morph region is a vertex group, and not a question.** The file lists a
  region's vertices whether they move or not, and the export used to take only
  the ones that move, so the import now keeps each region as a vertex group of
  its own. Which one belongs to which morph group is the addon's business: a
  group that came out of a file keeps the file's region, a group made here
  covers the vertices its targets move, and the panel says which of the two it
  is rather than asking. Picking a group brings its region up as the mesh's
  active vertex group, where Blender's own tools reach it.
- **A Single Morph with nothing to morph exports**, with an empty morph block,
  as 13 of the game's models have. It used to be refused.
- **Joints stay where they are put.** The import places each bone from the
  file's chain worked out in full precision. The export reads each bone's head,
  tail and roll exactly, switching into Edit Mode for a moment to do so, and
  writes the 32-bit position and rotation that put the bone exactly there.
  Joints used to shift a little on every round trip.
- **A mirror's view box is the file's own numbers**: four fields on the
  mirror, edited like a dummy's box (see *Mirrors*). It used to be a cube empty,
  whose transform could not hold the slight skew some of the game's boxes have.

Values a file works out from others are worked out the way the game's files
have them:

| Value | Worked out as | Game files it gives back |
|---|---|---|
| Skin box | The box of the vertices that follow the mesh frame | 522 of 526 |
| Joint a bone group blends with | Its parent joint, when the group has blended vertices; none otherwise | 9437 of 9468 |
| Bone group box | Its vertices carried through the bind matrix, in 32-bit arithmetic | 3950 of 8065 |
| Morph and mirror center | Low corner plus half the span, in 32-bit arithmetic | 181 of 184 morphs, every mirror |
| Morph and mirror radius | Half the diagonal, summed from the last axis, in 32-bit arithmetic | 181 of 184 morphs, every mirror |
| Portal normal | First edge crossed with the edge to the last corner, in 32-bit arithmetic | 1469 of 2720 to the bit |
| Portal offset | Measured at the last corner, against that normal | 2362 of 2720 |
| A joint rotation part of nothing | Negative zero | 15244 of 15460 |
| Morph target normals | Each vertex's faces' unit normals added up on the target's shape; the mesh's own normal where nothing around the vertex moves | 93905 of 108851 to a hundredth of a degree |

**What still changes**, because Blender cannot hold it and nothing is kept aside:

- **Joint positions and bind matrices, on the first export.** A bone lives in
  the armature's space, rounded to 32 bits there, so it cannot always sit where
  the file's chain puts its joint. The position written back is the one the
  bone stands for, a few rounding steps off the file's in about two joints of
  three; rotations come back exact in all but 9 models, among them the joints
  `Mise06c Tom01 pohar` holds in a pose. After that nothing moves. The skin's
  bind matrices and bone group boxes are worked out from the joints and change
  with them.
- **The order of a morph region's vertices.** 122 of the game's 449 regions
  list them out of order, and a vertex group holds no order.
- **Morph target normals the modeling tool made from a shape the file does not
  hold**: 14946 of 108851, nearly all in characters' faces. In `Radni01` the
  tool smoothed the upper and lower lip together in one target and not in the
  next, and turned normals by a degree or two where the file's shape does not
  move at all. The export gives every target the normals of its own shape, as
  it does for the other 86%.
- **Worked-out values no formula gives back**: 1251 portal normals (most
  differ only in the sign of a zero), 74 of 1123 sector boxes, and about half
  of the bone group boxes, even where the joints come back exact.
- **The last vertex of `4old071`**, which looks exactly like a copy made for a
  face naming a vertex twice and is written as the vertex it matches.
- **Four models from an older tool** (`!TommyHIGH2`, `!TommyHIGH3`,
  `!TommyHIGH4`, `Untitled`) work out their skin boxes, 31 bone group links and
  two hand morphs' centers another way.
- **The five empty meshes in FREERIDE** that hold one empty LOD are written
  with no LOD, the way the game's other 35 empty meshes are.
- **The file's save date** is the time of the export.

### Joints are an armature, where the game puts them

A model's joints now come in as an armature that holds the file exactly:

- **Every bone sits where the game puts its joint.** The file chains a joint
  onto the frame above it with every scale on the way down, so a joint scaled
  by 1.0506 puts everything below it 5% further out. Bones used to leave that
  scale out, which drew Tommy's skeleton shrunk inside his mesh: feet 4.4 cm too
  high, hands 4.2 cm short of the wrists. Posing bent the mesh around those
  wrong points too.
- **The armature stands on the skinned mesh's frame** and the joints hang
  from it, as they hang from the mesh in the file (see *No bone for the mesh
  frame* above). In `I04Delnik01+` the mesh hangs from the joint `Bone01`, so
  the character's armature hangs on `Bone01`, and turning `Bone01` carries the
  whole body.
- **Unusual skeletons come back as they went.** `I04Delnik01+` once arrived
  with its mesh hanging from a bone that did not exist, and `BobAut01`, with a
  lone joint beside its skeleton, refused to export, and so did `Mise06c Tom01
  pohar`, whose joints have no skinned mesh at all. All three now export frame
  for frame as they came: joints not below the mesh are an armature of their
  own at the model's origin.
- **A mesh skins with the joints below it, numbered the way the game numbers
  them.** A joint above or beside the mesh takes no part and is written with the
  number 0, as the game's own files do. The import used to look joints up by
  number across the whole file, which only picked the right one for
  `I04Delnik01+` because of the order its frames happen to be in.
- **Every joint has an influence box**, and a joint with no weights painted
  has its weights made from it. The box is the joint's sixteen numbers, which
  the game's own exporter made the skin weights from. It lives on the joint,
  as sixteen numbers on its pose bone, and is drawn in green over the bone it
  belongs to - nothing stands in the scene for it, so a skeleton of
  twenty-five joints is twenty-five joints and not fifty objects. Its local +Y
  is the axis the weight runs along, and because the numbers are the file's
  own, a box nobody touches is written back to the bit. On export a joint's
  painted weights go
  out as they are; a joint with none painted has them made from its box, the
  way the game's exporter made them:
  1. A vertex inside the box is shared with the joint's parent by where it
     sits along the box's Y - none of it the joint's at the -Y face, all of it
     at the +Y face. Where boxes overlap, a joint beats the joints above it;
     otherwise the larger share wins. Between joints that are no kin of each
     other - a left leg and a right - the box whose middle the vertex is
     nearer, in the box's own proportions, takes it.
  2. The rest of the mesh is claimed by growing those regions along its edges,
     a ring at a time: a vertex joins, wholly, the region that reaches it,
     unless it lies behind that joint's box. A vertex two regions reach at
     once goes to the one it is further into. Where the region of a joint no
     kin of that one is nearer along the surface, the nearer takes it: rings
     of edges run ahead wherever the triangles lean, and a skirt went to one
     leg.
  3. A piece of the mesh modeled apart - a shoe past the ankle, a collar
     round the neck - is cut off from the regions growing through the body.
     Where a region stops behind its box in a piece its parent's region never
     got into, what it stops at goes up the joint's chain, to the nearest
     joint it does not lie behind as well, and that joint's region grows on
     from there. Behind all of them, it is left to the mesh frame, as on a
     body in one piece.
  4. A piece no region gets into at all - hair, an ornament hanging clear of
     the body - follows, whole, the joint it rests on most: of its vertices
     within a centimeter of its closest approach, the joint the most of them
     are nearest, at that joint's middle share among them.
  5. Whatever else no region reaches follows the mesh itself.

  Vertices at the same spot count as one, and the vertices painted weights
  decide are left alone and stop a region growing through them. A piece is
  weighted from its own boxes and along its own edges, or from what it rests
  on when nothing got into it, so nothing is taken from the piece beside it:
  the body under a sleeve weights exactly as it would with no sleeve. With
  nothing painted, this gives back the weights of 105,638 of the 109,578
  vertex positions in the game's 289 skinned models (96.4%), 69 of the models
  entirely, and 477 of Tommy's 480. Pieces used to be weighted only where a
  box held some of them, so a character built from many was left partly
  behind with the mesh frame: `Pomni`, fitted and weighted from its boxes,
  had 600 of its 16,070 vertices following nothing but the hips, and has 55
  now. The game's own characters weight as they did, bar ten vertices their
  tool left on the hips that now go up the chain: six of `hen`'s, and four of
  `I04Delnik01+`'s at the neck, which follow its collarbones. A first try at
  this counted pieces within 5 cm of each other as joined; it let one piece's
  region take vertices from the piece beside it and gave leftovers near the
  head to the shoulders, and is gone. The ones it gives back worst, near two
  thirds - `MichelleLOW`, `I04Delnik01`, `pol12`, `Ralph` - look weighted by
  hand rather than from their boxes. The joint panel says where a joint's weights come from, holds the box's
  center, size and turn as plain fields, and has **Add Influence Box**,
  **Fit Influence Box** and **Remove Influence Box**; the armature's panel adds
  any that are missing.

  **A fitted box is built the way the game's own are.** The game's boxes sit
  on their joint - their centre a hundredth of the way toward the joint below,
  typically - and reach about a fifth of the way there either side, with the
  axis the weight runs along aimed down the chain: on Tommy that axis is
  within 11 degrees of the next joint for the median box. The box is where the
  joint bends; the flesh beyond it, toward the next joint, belongs to the joint
  outright. So a fitted box sits on the joint, aims at the joint below it, and
  reaches a fifth of the way there, as wide as the mesh around the joint -
  the part of it nearer this joint than any joint outside its chain. The
  last joint of a chain - a head, a hand, a foot - has nothing below it, so its
  box carries on the way its parent led to it, and is as wide as everything
  beyond it, since that is what it owns. A box added from the panel is fitted
  the same way; before, it copied the box of a joint with nothing linked to it,
  whose weight axis pointed straight up - ninety degrees off any chain that
  does not.

  On a custom character given Tommy's boxes, fitting them is the difference
  between a skin that works and one that does not. `Pomni`, with every weight
  cleared and made from the boxes: with Tommy's boxes the chest took the lower
  half of the head and 801 vertices were left following the mesh; with fitted
  boxes the neck takes the head and 600 were left - all of them pieces of mesh
  that touch nothing else, which a region spreading along edges could not
  reach; taking pieces as above, 86 are.
  In Pose Mode, the selected joint's box carries handles in the viewport: six
  grey ones on its faces to resize it, three colored arrows to slide it along
  its own axes and three rings to turn it, and it is drawn brighter than the
  rest. Leaving Pose Mode lets go of it - Blender keeps the bone selected, but
  in Object Mode a click picks the armature as a whole, so the handles used to
  stay on a joint nobody had picked, and could still be dragged. Every one of them writes straight into the joint's
  sixteen numbers, so what is drawn is what is written. A dragged face or
  arrow stays under the pointer: the drag is followed to the nearest point on
  the handle's own axis. It used to be measured against the axis drawn a whole
  metre out, which perspective shrinks on one side of a box and swells on the
  other, so two opposite faces followed the pointer at different speeds - in
  one test view, 72% too far on one side. The same goes for a dummy's and a
  mirror's box. The **4DS Model**
  sidebar hides every box at once, which changes nothing that is written, and
  a box turned or scaled unevenly stays that way - an uneven size is part of a
  box's shape, not something to even out.

  What the export asks of them:
  - A joint with no box and no weights painted is refused. There would be
    nothing to weight it from, and a joint that moves nothing is a joint the
    model does not need.
  - A joint painted by hand needs no box of its own. Nothing in the game reads
    one, so it is written the box a new joint is made with, and the report says
    which joints that happened to.
  - A joint with no weights painted whose box has an axis of no length is
    refused: no weights can come out of a box that holds no vertex.
  - A joint with more than one box is refused, since a joint writes one.
  - A box held to no joint weights nothing and goes into no model. The export
    says so and writes the rest.
- **A new joint is the game's own new joint.** Add ▸ 4DS ▸ Joint makes it
  unturned against what it hangs from, with its culling byte at 61 and the
  influence box `Bone01` in `I04Delnik01+` carries - the box the game's tool
  gave a joint nothing was linked to, a cube 15.1 cm across 8.1 cm below it,
  its weight running up. A joint on a new armature lands at the 3D cursor by
  its bone, with the armature object itself left at the world origin.
- **No more than 64 joints below a skinned mesh.** The game gathers the joints
  hanging below a skinned mesh, first child first in the order the file lists
  them, and stops at 64; each goes in the slot its number names, and slot N is
  moved by the skin's group N. The export now refuses a 65th instead of writing
  a model whose extra joints would leave vertices behind. The game's own models
  use 25 at most.
- **Joint scale stays a 4DS value.** A bone cannot hold scale at rest, so the
  value lives on the joint. Changing it moves nothing in Blender, neither the
  joints nor the mesh. The export divides it back out of the positions it
  writes, so the game shows what Blender shows.
- **Animations play as in game.** A 5DS key is measured against the joint's
  rest in the file's terms, so a key that moves a joint moves it by the same
  stretch the game applies, and a scale key is measured against the joint's
  own scale. Measured against the game's own arithmetic, joints and skinned
  vertices match to a thousandth of a millimeter:
  - Tommy walking.
  - `01ChuzeLajd`, which moves joints and sets Tommy's joint scale back to 1.0.
  - Delnik turned by `Bone01`.

  A track for the mesh frame drives the armature's own location, rotation
  and scale, measured from where it stands. An armature with no skinned mesh
  animated as an object gets a warning that the movement is not written, since
  it stands for no frame.
- **An armature's own location, rotation and scale stay at rest.** Where it
  stands is its mesh frame's place, kept in its Delta Transform; its own
  channels are for the body's movement, and at rest they move nothing. A move,
  turn or scale left on them is written nowhere, so the export refuses it and
  says which of the three was done. The fix is Object ▸ Clear ▸ Location,
  Rotation and Scale, which puts the armature back where its mesh frame is,
  with what hangs on it. An armature with no skinned mesh stands for no frame,
  so its Delta Transform has to be at rest too. Models opened from a file are
  unaffected, and an animation loaded onto one is set aside while a model is
  written.

**Making your own character** works the usual Blender way:

1. Build an armature, or start from the character skeleton preset.
2. Name the mesh `base` and bind it to the armature. **A character's mesh has
   to be called `base` for the game's own animations to work on it.** They
   move the body - a crouch, a side roll - through the frame named `base`,
   and 2,563 of them carry a track for it. A mesh called anything else is
   not moved by them: the limbs still bend, but the body stays where it
   stood. The mesh's own origin can be anywhere - at the world's, at its
   feet - since the frame is the armature's; the armature stands at hip
   height, where Tommy's frame is (see the preset below). The export warns about
   a Single Mesh or Single Morph called anything else, and does not refuse
   one: of the game's 322 skinned meshes, 313 are called `base`. The other
   nine are three custom characters, four birds called `Base` and two dogs
   called `a`, and the birds and dogs have animations of their own, keyed to
   those names.
3. Set it to Single Mesh.
4. Either leave the weights to the influence boxes - fit each joint's box
   around the stretch of limb it should bend, with its +Y face toward the
   joint's end of the limb - or parent the mesh with automatic weights and
   paint from there. The two mix: a joint with painted weights keeps them.
5. Before exporting painted weights, in Weight Paint mode use **Weights >
   Limit Total** set to 2, then **Weights > Normalize All**.

Every vertex may follow one joint, blended with that joint's parent at most, and
its weights have to add up to 1. For the spine and both thighs - the joints at
the top of the skeleton - that parent is the mesh frame, so a vertex there can
be shared with the mesh as the game's own characters are: its joint's weight,
and the mesh's share in the vertex group the Armature modifier names as its
Vertex Group, with Invert on. A vertex with no weight at all follows the mesh.

Blender's armature scales a vertex's weights to add up before it moves the
vertex, so weights that don't add up still look right in Blender. The game uses
them as they are and gives whatever is missing to the joint's parent. Automatic
weights often total 0.95 to 0.99 and spread a vertex over up to five joints. The
export therefore refuses any vertex whose weights don't add up to 1 and names
the fix: step 5. It used to let a lone joint weighted below 1 through, which
then moved differently in game. The export's options for more than two joints
and for unrelated pairs still tidy up what Limit Total leaves.

An armature built from the Add menu stands at the world's origin, so its mesh
frame is at the feet. For a character, select it - the armature or its mesh -
and press **Set Default Mesh Origin** in the 4DS Model sidebar tab: the
armature then stands at the default mesh origin - hip height, where the game's
animations move the body from, found from the character's own skeleton - and
not a bone or the mesh moves in the world. It works on any
skeleton - bones connected to each other, or an armature moved, turned or
scaled as an object, as rigs made elsewhere often are, whose move goes into the
bones and leaves the armature's own Location, Rotation and Scale at rest, the
way the export wants them. An armature carrying an animation of its own is
refused until the animation is unloaded, since the keys are measured from
where it stands. The export
warns about a character whose frame sits more than 10 cm from Tommy's - the
game's own sit within 6 cm - and says how far off every animation would carry
its body.

### A character skeleton preset

**Add > 4DS > Character Skeleton (preset)** builds Tommy's skeleton, taken from
`Tommy.4ds`, the player's own model. Every human in the game uses the same
eighteen joints, named and chained the same way. 221 of the 276 human models
also have Tommy's proportions to within a few millimeters. The rest differ
mainly in where the skinned mesh sits against the body.

What it makes, all at the world origin:

- **The armature**, standing where Tommy's mesh frame is - at hip height, a
  touch forward - with the eighteen joints where his are. `back1` and both
  thighs carry his joint scale of 1.0506, which makes everything below them 5%
  larger; the spine and both legs hang from the mesh frame. Each joint has
  Tommy's influence box, the one his weights were made from, so a character
  with nothing painted is weighted as his own mesh is.

  It makes no mesh. A character's mesh is the character's own: name yours
  `base`, set it to Single Mesh and bind it to the armature, with its origin
  wherever it happens to be. The game's character animations move the frame
  called `base` - crouching drops it from 1.08 m to under half a metre, a side
  roll swings it through 0.9 to 1.07 m - and a key sets the frame's place
  outright rather than adding to it. A mesh called anything else is not moved
  by them at all, and a frame resting anywhere but hip height is carried to
  the wrong place, so leave the armature standing where the preset puts it. A
  skin numbers its joints in the order of the mesh's own vertex groups, so
  groups added in Tommy's order number the joints as his are.
- **`gun1` and `gun2`** under the right and left hand, where held weapons attach.
- **`notify`**, which receives the animations' events, such as footsteps.
- **`blnd`**, which the animations move to join one onto the next.
- **`targetN`**, the target the head turns toward, linked to `neck`.

It is built by the same code that imports a model, so it comes out as importing
`Tommy.4ds` would, minus his mesh and materials. Unlike opening a model, it
leaves the scene's Animated Objects count alone. If a name such as `base` is
already taken in the file, Blender gives the new frame another name and the
preset warns you, since the game finds these frames by name.

### A model counting animated objects needs its .5ds, or scene2.bin cannot reach it

When a model's **Animated Objects** is above zero, the game opens a `.5ds` with
the model's name from the same folder as part of loading the model. If that file
is missing, the model still loads and appears, but the load as a whole reports
failure. A mission only records a placed model's frame names after a load that
succeeded, so every `scene2.bin` entry naming one of its frames — a light's
sectors, a frame switched off, a lens flare's settings — is silently ignored.
Nothing in game points back at the cause.

Export now warns about it: when Animated Objects is above zero and there is no
`.5ds` of the model's name next to the file just written (and the export is not
writing one there itself), the report says what the game will do and how to fix
it — put the animation beside the model, or set Animated Objects to 0.

**Check 4DS Scene** writes no file, so it looks beside the last model you
exported and gives the same warning. Before a first export it only notes that
the game will need the animation next to the model.

### The model itself has a sidebar tab

**Check 4DS Scene** and **Animated Objects** belong to the model as a whole,
not to whichever object happens to be selected, and they were in the object
properties editor behind exactly that. They now have their own *4DS Model* tab
in the viewport sidebar, at hand while the model is being built and unchanged
as the selection moves. The count still says whether it is following what the
scene has keyed or has been pinned to a number, and pinning still works the
same way.

The tab also carries **Make Weights From Boxes**, which paints onto the mesh
what the export would have made from the influence boxes: the joints with
nothing painted get their weights as ordinary vertex groups, and from then on
they can be looked at, weight-painted over, or left as they are. It makes
exactly what the export makes, so the file does not change by pressing it -
`Tommy` with every weight cleared exports the same bytes either way - and the
joints it painted count as painted from then on, so pressing it twice does
nothing the second time. A vertex shared with the mesh frame keeps the
mesh's share in the vertex group the Armature modifier names as its Vertex
Group; where the modifier names none, the button makes one called after the
mesh and sets the modifier to it, with Invert on, and says so.

Beside it, **Clear All Weights** takes every weight off the selected meshes,
so each joint goes back to being weighted from its box. The vertex groups
stay, emptied: their order is what numbers the skin's joints, so deleting them
would renumber the joints in the file. A morph region's vertex group says
which vertices a morph moves, not which joint they follow, so it is left
alone, and so is every mesh that is not selected. `Tommy` - a Single Morph,
whose morph regions are vertex groups - cleared with it exports the same
bytes as `Tommy` with every weight but those regions taken off by hand. It is not offered in
Edit Mode, where the mesh being edited would overwrite what it cleared.

The tab also carries the joint settings. **Joint Size** is a slider for how big
the joint markers are drawn: a size that reads well on a character buries a
car's door hinge and disappears on a building, so it belongs to the scene you
are working in rather than to the model. It multiplies the marker the joint's
own 4DS scale already sets, so a joint scaled 2 still draws twice the size of
its neighbors. **Influence Boxes** shows or hides every joint's box in one
press and says how many there are. Both are viewport settings and nothing
else: nothing about them is written, and a hidden box is still the joint's box.

### More flag switches, and the raw fields take hex

**New switches.** Target flags gained four: *Look At*, *Follow Rotation*,
*Follow Position* and *Follow Scale*.

Bits that nothing in the game reads get no switches, and neither do a sector's
light groups, which the game turns on for every sector once a mission loads.
They are kept exactly as they are and can be changed through the raw fields. The
light's Mode box still says in words how many unread bits are on.

**How the engine's own switches are shown.** The switches the game reads sit in
the grid as before. Of the engine's own, any this file has switched on are
listed openly, as they always were — a flag a model really uses is never
hidden — and the rest are left out of the panel altogether, since they are bits
nothing reads and the raw field above still reaches every one of them.
**Show Engine-Reserved Flags** in the preferences lists all of them openly
instead.

**Target flags** used to be a bare number. They are four switches now. Look At
turns every linked object to face the target, and while it is on the follow
switches are not read — the panel says so when both are set. A linked object
with no parent copies the target's whole transform as soon as any follow switch
is on.

**The raw fields work the way they read.** Each shows its whole value in hex,
and whatever is typed back is read as hex too — with or without `0x`, with
spaces or underscores anywhere. Before, typing `FF68` without the `0x` did
nothing at all. A value that does not fit the word (a second byte in a frame
flag byte, say) is refused rather than cut down. The frame, rendering, visual,
portal and target flags are hex fields now as well, where they were plain
numbers, and the light's field is called **Raw Flags** and sits at the top of
the Mode box like the others.

**Show Raw Flag Values is on by default,** as this guide already said it was;
the code had it off. A preference you saved earlier keeps its value.

The animation panel's key flags are the one word left as it was: import and
export both refuse a track with a bit the game does not know, so there are no
switches for those bits.

### Environment flags are numbers, and Diffuse MipMap says what it does

**How the environment settings are stored.** The low bits of a material's flag
word are not a row of switches. Three runs of them each hold a small number:

| Bits | Holds | Edited with |
|---|---|---|
| 0-7 | How many times the environment texture repeats (0-255) | Env Tiling |
| 8-10 | Which blend the environment texture uses (0-7) | Env Blend Type |
| 12-14 | How the environment texture's coordinates are made (0-7) | Env Mapping Mode |

Bit 11, between them, is a switch of its own (Texture Manager). The older
script named each of bits 8-10 and 12-14 as if it were a separate switch —
Overlay, Multiply, Additive, Global Reflection, Project Y, Project Z — and bit 0
as "unlit". Those names were wrong: Multiply, for instance, is the number 3,
which is bits 8 and 9 together, so it read as Overlay and Multiply both on. The
seven hidden checkboxes that carried those names are gone. Nothing is lost:
every bit of the word is still edited by a real control, which the tests check.

**Diffuse MipMap.** Bit 23 was labeled *Env MipMap* and sat in the Environment
Mapping box, but it controls mipmaps for the **diffuse** texture — the
environment texture is mipmapped either way. It is now called *Diffuse MipMap*
and sits under Texture Loading in the Diffuse box. On a color-keyed material it
also makes the game draw the material alpha-blended.

**Types that do not exist.** A model naming frame type 0 or 18 and up, or visual
type 9 and up, now fails with a message saying that type does not exist and the
game refuses the file — rather than calling it merely unsupported.

### Light controls, and what the mode bits and fog values really do

**The marker points the way the light shines.** A light is a **Cone** empty.
Blender's cone points along the empty's local +Y, which is the way a light
shines, so the marker and the beam agree.

**Handles for the range and the cones.** Select a spot light and four gray boxes
join the aim ball: one on the beam at the near range and one at the far range,
which slide along it, and one on the rim of each cone where the beam ends, which
slide out across it — the outer cone toward local +X, the inner toward +Z. A
point light gets the two range boxes. Each box drags one stored value and
nothing else, and none drags past its partner: near stops at far, the inner cone
stops at the outer, and a cone stops just short of a half turn, which has no rim.
The aim ball on a spot now sits a few pixels past the far handle so the two never
overlap. A spot also draws a ring across the cone at its near range.

**A sixth mode bit.** Bit 4 (0x10), **Lights Lit Objects**, lights Lit Object
meshes while the game runs, on top of their baked lighting. It is off in the
engine's own default; the game turns it on for its car headlights and fire
lights. Ordinary objects, billboards and morphs ask for bit 0 instead, which is
now labeled **Lights Objects** rather than *Real-time Lighting* — a light with
neither lights nothing in game, and the panel says so. The panel also shows which
of the other bits are set; bit 2 and bits 7 and up are never read and are kept
exactly as they are.

**What the fog kinds do with their values.** The three atmosphere kinds are all
fogs, drawn in the light's color — Power and the cones are not read:

| Kind | Near | Far |
|---|---|---|
| Fog | Where the fog starts, as a percentage of Far | Where it is thickest, as a percentage of the view distance |
| Point Ambient | Density of an exponential fog, as typed | Not read |
| Point Fog | Density of an exponential-squared fog, as typed | Not read |

The panel labels the fields accordingly — *Start %* and *Thickest %* for Fog,
*Density* for the other two — and works out Fog's figures in words. The old
description, that these kinds' ranges are scaled by 1/100 and set the sector's
"ambience", was wrong, and so were the rings the viewport drew for them: their
values are not distances, so nothing is drawn for them now. **Ambient** adds its
color, times its power, evenly to everything in the sectors it lights, and
**Layered Fog** draws no fog at all — the game only counts it toward how dark
shadows are.

**Where a light works.** The panel now says it: a light only lights the sectors
that list it, and that list is in the mission's scene data, not in the model — so
a light in a model on its own lights nothing.

Under that note, a folded **Switching It On in scene2.bin** section (closed until
you open it) lists what a light in a model needs to work in a mission, which
file each requirement goes in, and which kinds of light it is for. Rows that do
not apply to the selected light's kind are dimmed. *Lighting kinds* are Point,
Spot, Directional and Ambient.

| File | Requirement | For |
|---|---|---|
| Model `.4ds` | Animated Objects 0, or a `.5ds` named like the model beside it — otherwise the mission never finds the model's frames (the row turns into a warning while the count is above zero) | All kinds |
| Model `.4ds` | Enable on | All kinds |
| Model `.4ds` | Power not 0 | Lighting kinds, Layered Fog |
| Model `.4ds` | Lights Objects on, to light meshes | Lighting kinds |
| Model `.4ds` | Lights Lit Objects on, to light lit objects | Lighting kinds |
| Model `.4ds` | Affects Shadows on — shadows are all a Layered Fog affects | Layered Fog |
| `scene2.bin` | Placed after the object that places the model | All kinds |
| `scene2.bin` | A frame named `<model object>.<light name>` with no frame type (the name alone for a light in the mission's own `scene.4ds`) | All kinds |
| `scene2.bin` light datablock | Sector — each sector it lights | All kinds |
| `scene2.bin` light datablock | Color or Power — without either the light is white at power 1; fogs use the model's color as it is | Lighting kinds, Layered Fog |
| `scene2.bin` light datablock | Range or Cone — without either a spot keeps the default reach | Spot |
| `scene2.bin` light datablock | Range — without it a Layered Fog keeps the default range | Layered Fog |
| `scene2.bin` light datablock | Mode, when the same model file is placed twice in a mission — the second copy gets the default mode | Lighting kinds, Layered Fog |

The mode panel's note about switches the game never reads now gives their number
in words instead of as a hex value, and the animation panel's warning about an
unknown flag no longer prints the value either.

### Textures: BMP and TGA only, and a button to fix the files

**Only BMP and TGA textures are accepted.** They are the only two formats the
game reads, so:

- A texture slot's picker lists only BMP and TGA images.
- An image of any other kind put into a slot another way - opened into it, or
  set from a script - is taken straight back out, with a message saying why. So
  is an image whose name has no extension at all, since the game could not load
  that either.
- An imported model that names a texture of another kind gets no texture in that
  slot, and the import log says so.

**Fix Texture File.** When the file behind a texture slot is one the game would
misread, the material panel says what is wrong under the slot and offers **Fix
Texture File**. It rewrites the file in the one form the game's loader reads,
under the same name:

| Problem | What the fix does |
|---|---|
| BMP header longer (or shorter) than 40 bytes | Writes a 40-byte header; the pixels are copied across untouched |
| BMP pixels not straight after the header and palette | Moves them there |
| BMP rows stored top to bottom | Stores them bottom to top |
| RLE8 or RLE4 packed BMP | Unpacks it to plain 8-bit, same palette |
| 1, 2 or 4-bit BMP | Makes it 8-bit, same palette |
| Bit-field BMP (16 or 32-bit) | Unpacks it to 24-bit, or 32-bit when it has alpha; layouts the game already reads are left byte for byte |
| Run-length packed TGA | Unpacks it |
| Right-to-left TGA | Turns it left to right |

A paletted texture always stays paletted: an 8-bit texture's color key is the
first entry in its color table, so making it 24-bit would lose its transparent
color. Sizes that are not a power of two are not changed — resizing is a quality
decision, not a repair.

**Fix Frame Files.** For an animated texture, the Texture Animation box offers
**Fix Frame Files** under Play and Pause when any frame file needs it. It fixes
every frame in the range the Frames fields set — the diffuse frames, the alpha
frames when the alpha is animated, and the environment frames — and no frame
past it.

Both buttons show what they will rewrite and ask first. Each original is copied
into a `texture_backups` folder beside it before it is replaced; a later backup
of the same file takes the next free number rather than overwriting the first.
The images in Blender reload straight away.

The Color Key flag's description now also says where the key color comes from:
the first entry in an 8-bit texture's indexed color table.

### Smaller fixes

- **Light handles and outlines show up as soon as you change them.** Blender
  does not repaint the 3D viewport when an add-on setting changes. So a light
  switched to Point or Spot showed no range handles, and a new range, cone,
  color, dummy box or projector shape kept its old outline, until something else
  repainted the view, such as an export. Those settings now ask the viewport to
  repaint.
- **A lens flare keeps the size you give it.** Every viewport refresh used to
  shrink its sphere back to 0.05. That size is now only set when a flare is made
  or imported, as with projectors.
- **Environment blends 0, 6 and 7 are warned about.** With an environment
  texture on, the game only has a blend for values 1 to 5 (Blend by Amount,
  Blend by Alpha, Multiply, Multiply Twice and Add). For None, or the unnamed 6
  and 7, it logs an error and draws the reflection with whatever blend the
  surface before it used. The export now warns about that, the preview shows no
  reflection for those values (it used to show Multiply for 6 and 7), and the
  None option's description says so.
- **The Sector panel explains harmless engine bits.** When a sector has Visible,
  Enumerated or Visibility Cycle on, the panel says the game redoes those every
  frame, so their stored value does nothing.

### Sound and Area frames are skipped on import

A model containing a **Sound** (type 4) or **Area** (type 14) frame used to be
refused outright. It now imports, with those frames left out and the import log
saying which ones were skipped.

Neither can be supported properly, because the game never loads one from a
file. It builds sound sources and sound areas itself while it runs, and for
either type it reads only the frame type and parent id. There is no name, no
position and no settings to read, so a Sound or Area frame is three bytes in a
file and nothing more. There is nothing in one to import, and nothing that could
be written back that the game would use, so an export never contains one.

What happens on import:

- Each skipped frame appears in the frame list as *Sound - skipped* or
  *Area - skipped*, and one warning names them all.
- A frame that hung from a skipped one now hangs from the frame above it. It
  stays exactly where it was, because the game places a Sound or Area frame
  right on its parent with no offset of its own.
- A target that links to a skipped frame loses that link, with a warning.
- If a file has more bytes after a Sound or Area frame than the game reads —
  say, a tool wrote the usual frame header there — everything after it is out of
  step, in game as well. The import still fails then, but the error now points at
  the Sound or Area frame as the likely cause.

### Dummy boxes are linked to their empties

A dummy box that is not centered on its empty now has a dashed line from the
middle of the box to the empty, in the box's color, so in a crowded scene you
can see which box belongs to which empty. The line follows the viewport's
**Relationship Lines** overlay toggle, like Blender's own parent lines, and its
dashes stay the same size on screen at any zoom. A centered box gets no line,
because it already surrounds its empty.

### Selected Objects Only covers the whole export, not just the file

With **Selected Objects Only** ticked, the file already held only the selected
objects, but everything around it still looked at the whole scene. An animation
export warned about eased keys and long rotation turns on objects you had left
out, and an armature you had not selected, sitting outside the movement track,
could refuse the export outright. A model exported with **Write Its Animation**
wrote every animated object in the scene into the `.5ds` beside it, including
ones the model did not contain.

Now every check and every message is about the objects being exported:

- **5DS** — the easing, long-turn and movement-parenting checks look only at the
  selected objects.
- **4DS** — the `.5ds` written beside the model holds the selected objects'
  tracks only. The animated object count is compared against the selected
  objects too, so if the scene's count covers more than you selected, the export
  now says so: *The model says it has 2 animated object(s), but 1 of the things
  being exported are animated.*
- **6DS** — the report starts with how many objects were selected, as the model
  and animation exports already did.
- **TCK** — a movement track exported on its own has no Selected Objects Only
  option, because a scene has one movement track and there is nothing to choose.
  Written beside an animation, it follows that export's selection as before.

### Instancing is shown in the object panel only

The instance labels in the 3D viewport ("master (3 copies)", "copy of lamp_post")
and the faded object color given to copies are gone, along with the Instance
Tags setting that controlled the labels. Keeping them accurate meant working out
the whole scene's instance plan again after almost any edit, including every
vertex moved in Edit Mode, and on a large scene that cost more than they were
worth.

What remains is the **Instancing** box in the object panel, and it now covers
models you build yourself. A linked duplicate made in Blender (Alt+D) is named
**COPY** with its master, or **MASTER** with its copies, exactly as an imported
instance is. It used to say only that one of the objects would own the mesh on
export. An object that shares a mesh but is written out with its own copy — a
skinned or morphing mesh, or one with LOD levels of its own — is shown as
**Written In Full**.

The box asks the question the way the export does: the same frame order, the
same rule that the first object using a mesh owns it, and the same move that puts
the object with the most LOD levels first in its group. What it says is what the
file will hold; on the game's mission scenes it matches the files exactly. The
Master field used to be editable, but the export never read it, so it is now just
the name. The import no longer records which object an instance came from either
(the old `ls3d_instance_source` property is gone): nothing read it once the
viewport labels were removed, and the panel and export work it out for
themselves.

Along the way the export itself got faster on large scenes: ordering the frames
of a 1,700-object mission took almost half a second and now takes about 35 ms,
with the order unchanged.

### Turns of half a circle or more play the right way round

The game blends between two rotation keys by always taking the **shorter** way
round — if the two rotations point into opposite halves it flips one first,
every time. So a turn of 180 degrees or more between two keys comes out the
other way in game, and a whole turn not at all, however smoothly Blender plays
it. A ring that dials back and forth is exactly the animation that hits this:
turn +286 degrees between two keys and the game turns -74.

**Fix Long Rotation Turns** fixes it. It is an export option, in the 5DS export
dialog under *Rotation Corrections* and in the 4DS export's *Animation* box
beside *Write Its Animation*, the same shape as the weight fixes.

Where an Euler or axis-angle rotation turns half a circle or more between two
keys, it evaluates your curve at evenly spaced frames in between and adds those
as keys, so no step is more than a quarter circle. For a turn about one axis
that reproduces Blender's own blend exactly — both move at an even speed from
key to key. The export report says how many keys it added and where.

It is **off by default**, like the weight fixes, so an export writes your keys
exactly as keyed unless you ask for more. With it off, the export warns about
each turn that will play the other way and names the frames, and Check 5DS
Animation does the same. Tick it when the warning appears — it changes nothing
in Blender, and every key it adds is read off a curve you already made.

Two things it deliberately does not do:

- **It never splits a quaternion curve.** Those are already in the file's own
  terms, and the game's own animations store thousands of neighboring keys with
  their signs flipped — a turn of almost nothing that Blender's preview blends
  the long way round. Splitting along that would make a re-exported game
  animation spin where the original barely moved. Re-exporting one adds no keys.
- **It cannot split a turn with no frames to spare.** Keys sit on whole frames,
  so half a circle squeezed into one frame has nowhere to put the key it needs.
  That case is still a warning.

### Unload Animation, and an animation never reaches the model

**Unload Animation**, under the list in the 5DS Animation tab, takes every
animation off the scene: the timeline is blank, the skeleton is back in its
rest pose, the body back where its armature stands, and every object the
animation moved back where the model has it. The animations stay in the list,
and **Make Active** puts one back on.

That needed a model to keep where each thing rests while an animation plays,
and it used to lose it. An animation keys an object's own location, rotation
and scale, and those then hold whatever frame is showing: after loading
`01ChuzeLajd` onto Tommy, `blnd` - which the walk moves - had nowhere left that
said where Tommy has it. Now an object keeps its rest in its **Delta
Transform**, as an armature keeps its mesh frame: loading an animation onto it,
or starting one with **New Animation**, moves where it stands there first,
without moving it, and the keys are measured from it.

The model export also takes animation off for real now. It meant to all along,
by turning the animation's influence to nothing - but with no NLA stack under
it Blender plays an action at full strength whatever its influence says, so a
model exported with an animation loaded went out at the frame showing: the
skeleton written bent as if posed by hand (Tommy's face morphs made that a
refusal instead), and `blnd` wherever the walk had it. Tommy exported with the
walk on at frame 20 now writes exactly what Tommy exported as opened does, and
so does Tommy after Unload Animation. A pose made by hand, on bones the
animation does not key, is still the model's, as before.

### Picking an animation no longer moves the scene range

Make Active used to set the scene's frame start and end to the animation it put
on. It no longer touches either. The range is the user's setting, several
animations can be on at once, and — the part that made this more than a nuisance
— the 5DS export reads that range, so picking an animation quietly changed what a
later export would write.

The panel already said when the range fell short of the keys. That warning now
carries a **Fit Scene Range To Keys** button beside it, the same shape as the
frame-rate fix above it, so it is one click when you want it and nothing when you
do not. It fits to every animated frame in the scene rather than to the one
animation that happens to be picked.

### The animated object count takes zero

Any value above zero sends the engine looking for a `.5DS` named like the model
and makes the result of opening it the result of loading the whole model — so
zero has to be sayable, and that is the state a model is in while its animation
is still being built. The count followed the scene upwards, and the handler put
the number straight back on the next depsgraph update, so typing zero did
nothing.

It now follows the scene until you type a number it would otherwise overwrite,
and then stays where you put it. The panel says which it is — "Following what the
scene has keyed", or a pin icon that hands it back to the scene. Only the number
reaches the file; whether it was typed or counted is authoring state.

### Animated textures pick up frames that arrive later

Setting up an animation before its frame files were in place used to leave it
broken for the rest of the session. The first lookup stood frame 00 in for every
file it could not find and that answer was cached for good, so copying the
frames in changed nothing on screen — the only way out was to restart Blender.

Two things were wrong. The lookup now caches only a **complete** set, so an
incomplete one is tried again; and the folders it searches are indexed by their
modification time, so files appearing in one are noticed without anything having
to be told. Twenty frames now cost two directory reads rather than twenty, and a
set that is already whole costs none.

The guard meant to spot a frame whose image had been deleted was also broken: it
used `getattr(frame, "name", None)`, and a default covers a missing attribute
rather than a dead reference, so reaching a freed datablock raised instead of
being noticed. Opening another file frees all of them at once, which made that
the normal case rather than an edge one.

### Texture checks

Check 4DS Scene, every export and every import now look at the texture files
themselves, not just their names. On import each texture is checked once, when
it is found in the texture folder, however many materials share it. The game's image loader is narrower than anything in Blender
suggests, and a texture it cannot read spoils the surface without spoiling the
model file — so these are reported, never refused.

What it catches:

- **A BMP whose info header is not 40 bytes.** The loader reads exactly 40 and
  then takes pixels from wherever the stream sits, so a BITMAPV4HEADER (108) or
  BITMAPV5HEADER (124) leaves the difference in front of the pixels and slides
  the whole image — about 28 pixels for a 640-wide 24-bit V5 file. Every UV on
  it then lands somewhere else, while Blender shows it perfectly. This is the
  one that looks like broken UVs and is not.
- **Compression.** Anything but 0 is refused outright by the loader.
- **Bit depths other than 8, 16, 24 or 32.** The loader cannot expand them.
- **Anything that is not `.bmp` or `.tga`.** The loader picks the format from
  the first letter after the last dot and gives up on the rest, so `.png`,
  `.jpg` and `.dds` are not textures as far as the game is concerned.
- **Run-length encoded or right-to-left Targas**, which the loader reads
  straight through without unpacking or mirroring.
- **Sizes that are not a power of two**, width and height checked separately.
  The game rounds each side up to the next power of two on its own and enlarges
  the image to fit without smoothing, so the message says which side is off and
  what each is rounded up to: *is 640x640: width 640 and height 640 are not a
  power of two, the game rounds them up to 1024 and 1024*.
  A note rather than a fault — the game ships 35 such textures itself. (With
  the game's Low Detail Textures option on, each side is halved before that
  rounding, unless the material has No Presaved Texture set.)
- **An animated texture the frame counter cannot number.** The game builds each
  frame's name by writing two digits six characters back from the end, so the
  name has to look like `WATER00.BMP`.
- **An image pointing at a file that is not there.**
- **A BMP whose pixels do not start straight after its header and palette**, or
  whose rows are stored top to bottom (a negative height). The loader reads the
  pixels from right after the palette and cannot read top-to-bottom rows at all.

Every problem above except size and a missing file can be fixed from the
material panel — see *Textures: BMP and TGA only* above.

All of it was measured against the 4651 textures the game ships: none of them
is flagged for anything but size.

### What changed since 0.7.0

0.7.0 handled models. This build handles the three formats that sit beside
them, and was reorganized and renamed to match.

**Animations (`.5ds`).** Import and export, with the animation matched onto
whatever model is in the scene by track name. 2726 of the 2734 the game ships
round-trip byte-for-byte; the other 8 are older format versions. Each animation
becomes one action per object it drives, listed in the sidebar where it can be
picked, renamed, started or deleted.

* **Event cues** — the numbered markers the game acts on as an animation plays,
  29 of them decoded by name. They live on the `notify` Dummy, which is the
  only frame the game reads them from, so that is the only place the addon
  will put them and the export refuses them anywhere else. Two cues may share
  a frame, as a few of the game's own animations do.
* **Named events**, the string-carrying kind, are carried too. Nothing the game
  ships uses them and no game code reads them, but the format holds them.
* Keys are linear by default, since the game walks straight from one to the
  next, and the export says so when they are not.

**Movement tracks (`.tck`).** Where an actor travels while an animation plays,
carried on an empty above the model. 283 of 293 round-trip; of the rest, five
are an older version and five have a nonsense version number. Written beside
the animation under the same name, and one per scene, because the file holds a
single track.

**Shadows (`.6ds`).** The coarse mesh the engine casts from, one piece per
frame of the model. All 37 the game ships round-trip byte-for-byte. The format
was worked out from scratch — nothing else reads or writes it.

**Reorganized.** Each format owns a folder, with what they share underneath;
see *Layout* above. The four largest modules were split along the seams they
already had.

**Renamed** from *LS3D 4DS Importer/Exporter* to *Mafia Toolkit*, and the
package folder from `io_scene_4ds` to `io_mafia_toolkit`. Installing this
version leaves the old one registered separately — remove it, or both will
appear in the File menu.

**Material preview.** The shader graph behind the 4DS material panel was
rebuilt to draw what the game draws. See *Transparent materials* below.

**The console names what it is working on.** Every format prints a line per
item as it goes — `[Mat 02/11] WOOD.BMP 0x40808001`, `[Frame 07/29] 'l_hand'
Visual/Object`, `[Track 12/20] 'r_shoulder' 319 rotation`, `[Piece 03/28]
'l_foot' 25 vertices, 19 triangle(s)`, `[Movement 1/1] 827 sample(s) every 40
ms`. A run that stops partway now says which item it stopped on rather than
only which stage, and the counts are padded so the names line up in a column.
The largest scene in the game prints 2428 of these and imports no slower for
it.

**Animated textures play in the viewport.** A fire, a water surface or a
lightning bolt steps through its frames on screen, on a timer of its own — the
game's frame times are milliseconds and have nothing to do with the scene's
timeline, so neither does this. **Nothing is keyframed:** no keys are inserted,
no action is created, and a scene that was not animated before is not animated
after.

What steps is the image on the preview node. The material's own texture stays
on frame 00 throughout, because that is the name the export writes — parking
playback on frame 07 and exporting would otherwise write `FIRE07.BMP` where the
model said `FIRE00.BMP`.

Nothing plays until you ask it to — opening a model does not set every fire
and water surface running. **Play** and **Pause** sit above the frame fields:
Pause holds the frame it is showing and Play carries on from there, and a
paused animation stays paused while you adjust the settings rather than jumping
back to life at the first nudge. Switching *Anim Diffuse* off stops whatever is
running and puts the texture back where it started; the frame count, frame time
and loop frame are untouched, so they are still there when it goes back on.
Wrapping follows the game, back to the loop frame rather than to the beginning,
and a loop of -1 picks a frame at random.

**Target frames can be muted one at a time** from the 5DS sidebar. A target
frame's Track To beats whatever the animation says, so the panel already had a
scene-wide *Ignore Target Frames* switch — but that is the blunt instrument,
and usually it is one frame fighting the animation. Each target frame is now
listed there with its own switch and how many objects it steers. The scene-wide
switch still wins while it is on, and the per-frame rows gray out rather than
disappearing, because what each is set to matters again the moment it goes off.

**The morph panel moved to the sidebar**, beside the 5DS animation one, under
its own *4DS Morph* tab. Morph targets are shaped in the viewport — pulling
vertices about and watching the blend — rather than set up once in the
properties editor and left alone. The tab stays put as the selection changes
and says what is missing instead of vanishing, and it names the mesh whose
groups it is showing, which the properties editor made obvious and a sidebar
does not.

**Projectors.** The frame type that paints a material onto whatever surfaces it
covers — headlights, bullet holes, a puddle of light on a floor — is now read,
written, edited and drawn. No model the game ships uses one, so this is for
files you author yourself; the format has always carried it.

A projector has no mesh. Four bytes describe it, and the volume it covers is a
fixed unit shape the frame's own transform carries, so it imports as an arrows
empty — small, and labeled, so you can see which way local +Y points — and the
viewport outlines the volume from the transform: set to *Perspective*, a
pyramid spreading from the object's origin; set to *Orthographic*, a box the
same width all the way through. Either reaches one unit along local +Y and two
across, so the object's scale sizes it. Nothing about the shape is stored, on
either side.

That outline *is* the object — there is nothing else to see — so it follows the
selection the way any other object's wire does: black at rest, and the theme's
orange once picked, taking the active and selected shades from your own theme.

The whole payload is four bytes:

| bytes | field |
|---|---|
| 1 | straight-on, or spreading from the origin |
| 1 | mode |
| 2 | material, 1-based into the model's material table |

The mode byte is **one of six settled values, not a bit field** — the game
compares it against each in turn rather than masking it — and two things come
out of it, which the panel shows as two dropdowns.

**Depth Falloff** gives each projected vertex a weight from its depth through
the volume, normalized to 0 at the near face and 1 at the far one:

| | weight |
|---|---|
| Constant | `1` |
| Linear | `1 - depth` |
| Triangular | `1 - 2 × abs(depth - 0.5)` |

**Blend Mode** decides where that weight lands and how the result meets the
surface:

| | result | the weight drives |
|---|---|---|
| Additive | `src + dst` | the vertex RGB, alpha forced opaque |
| Alpha Blend | `src × a + dst × (1 - a)` | the vertex alpha |

Additive can only brighten, so black parts of the texture add nothing — that is
light. Alpha Blend covers in proportion to the weight — that is a mark. The
game's own headlights and railway lamps are Linear + Additive; its tyre tracks
and car scratches are Constant + Alpha Blend. Both paths draw the diffuse
texture modulated by the vertex color with alpha testing on, so a texel with
zero alpha is discarded either way.

The six valid values do follow a tidy pattern, but nothing reads them that way,
and a byte outside them is not split down the middle — all three tests the game
makes ask whether the mode is one of the additive ones, so an unknown value
lands on the alpha-blended side of every one and picks up no depth term. It
behaves as mode 1 exactly: Constant + Alpha Blend. That is what the panel shows
for one, and the stored value is written back unchanged until you touch either
dropdown.

**What it paints with** is the material's *diffuse* texture and nothing else —
no environment map, no alpha texture, no second stage. An animated diffuse
material steps its frames while it projects. A material with no diffuse texture
makes the game skip the projector entirely, so the export warns about it. The
material is picked in the panel, since a projector has no mesh and so never
appears in Blender's Material tab — and its 4DS settings are edited there too,
under a *Material Settings* disclosure that stays folded until you want it, the
same arrangement a lens flare's element materials use.

**A projector's material id is never allowed to be 0 or below.** The game
subtracts one from the stored index and follows the result without checking it,
so an empty slot is not a projector that draws nothing — it is a pointer read
from the word in front of the material table, with a reference count written
through it. The export refuses a projector with no material, refuses one whose
material never reached the model's table, and the format layer refuses to write
an id below 1 whatever asks it to.

**What is not in the file**: how brightly it paints, what color it tints with,
how far from the camera it still draws (fixed at 100 units, scaled by the
camera's field of view), and how much of a back-facing surface it accepts. The
game sets those in code and the format has nowhere to put them.

**Flag bytes.** A projector reads nothing from the second render-flags byte —
shadows and projections are settings on the surfaces that *receive* a
projection, and the receiver walk only ever collects the five object visual
types, which a projector is not. So a new one is seeded with both flag bytes at
zero rather than the `0x22` a mirror carries, and the renderable-object bit is
cleared on export whatever it says.

One flag is decided by the type rather than left to you: a projector is not one
of the engine's mesh objects, and that bit tells the game to read the frame's
own fields as a mesh pointer and follow it. The export clears it, the way it
already does for mirrors — those two are the only visual kinds outside that
family. Which surfaces accept a projection is set on the surfaces, by
*Projection on Diffuse* in their own Visual Flags.

**Lit objects.** The last visual sub-type a model can name is now read and
written. A lit object is a mesh with baked lighting, and the model stores
*exactly* what a plain object does — the bake is not in the `.4ds` at all. The
mission that places the object carries it, swapping the plain object for a lit
one at load time and then feeding it the lightmaps. That is why no shipping
model declares one: all 3200 of them store plain objects, and the conversion
happens in the mission. Supporting it means a file that does declare one opens
correctly rather than being refused; the panel says where the lighting lives so
nobody goes looking for it here.

**Emitors.** The particle-emitter frame type is read and written. What the
model stores is the frame and one mesh, in the same block a visual's geometry
uses — and that mesh is the **particle**, not the emitter: every particle it
spawns is a copy of it. None of the thirty-odd settings that make it emit —
rate, lifetime, direction, spread, speed, gravity, size and color from birth to
death, additive or normal blend — is in the format at all. The game sets them in
code, so an emitor authored here loads and holds its mesh but emits nothing on
its own; the panel says so rather than implying otherwise.

One rule is enforced rather than offered: an emitor is **never** written as the
source of an instanced mesh. The game resolves an instance by casting the source
frame to a mesh object and reading the pointer at that class's mesh offset,
which on an emitor lands on a particle setting instead. Two emitors sharing a
mesh each write it out in full, and a visual sharing it owns it.

**Lights.** The light frame type is read and written. Forty bytes carry the
whole of it: a mode word, which of nine kinds it is, a power multiplier, a color,
a near and a far range, and a pair of cone angles. Where it shines from and which
way it faces are not in those bytes — they are the frame's own transform, which
is why a light needs no direction field.

It imports as a cone empty, and the cone points the way it shines. The viewport
draws what the kind actually does: a point light gets rings at both radii on all
three axes, a spot gets its inner and outer cones out to the far range with the
axis down the middle and a ring at the near range, and a directional light gets
an arrow. The fogs and the kinds that light nothing by position draw nothing,
since none of their values is a distance from the light.

Blender's own light object holds none of this. Of the eight values it can carry
exactly one without loss — the color — so the panel owns all eight rather than
keeping a datablock half in step. Power is not a physical quantity: the engine
multiplies it by the color and uses the result. The two ranges fade linearly
between them, which is not what a Blender light does. Both cone angles are the
**full** angle in radians, not the half-angle; the engine halves them itself.
Six bits of the mode word are read — lighting ordinary objects, lighting lit
objects, the vertex bake, the bake's occlusion ray, shadows and lightness — and
they get checkboxes; the rest are never tested anywhere and are carried through
exactly as they came.

**Aiming a light.** A spot or a directional light shines along its frame's
forward axis, which is Blender's local **+Y**. Select one and a ball appears on
the end of the beam — the middle of a spot's far cap, the point of a directional
light's arrow. Drag it and the light turns to face wherever you drag to, the way
you would aim a Blender light. Only the rotation changes: the position, the
scale and all eight values are left exactly as they were, because the rotation is
the one thing a direction can be written into. A parented light gets the right
local rotation, with the parent's own turn taken back out of it.

There is a button beside it for when dragging is not what you want — *Aim At
Target* points the light at the one other object you have selected, or at the 3D
cursor when the light is all that is selected. Two other objects selected is
ambiguous, so it says so and leaves the light alone.

A point light shines every way at once and the other six are not shading
anything, so none of them is offered the handle.

The type in the file is a whole word and the engine's own switch has a default
case that does nothing, so a file naming a tenth kind still loads and that light
simply has no effect in game. The word is therefore kept as it came rather than
rounded into the list: the dropdown reads it as *None*, which is what the engine
makes of it, the panel says what the file actually said, and picking from the
list replaces it with a word the engine knows.

Nothing about a light is recomputed. The warnings are about what the game will
do with it, not about the file: a light with no real-time bit does nothing while
the game runs, one at zero power or zero far range lights nothing, and an inner
cone wider than the outer one leaves the beam with no soft edge. Each is a
warning because each writes a perfectly valid file.

No model the game ships has a light frame in it, so unlike every other block in
this addon the corpus cannot vouch for this one. The tests stand in for that
instead: the block is checked to be exactly forty bytes, every field is read back
out of the raw bytes by hand so that swapping two same-sized fields cannot pass,
and a light frame with nothing set stops the export rather than being written as
forty zeroes.

**The frame types that store nothing.** Camera, User, Model, Volume, Scene,
Shadow and Landscape are now read and written. There is nothing to decode: none
of their engine classes overrides the load, so each takes the base one, which
stops after the transform, the culling byte, the name and the user properties.
A frame of one of these types is a named point in the hierarchy and no more, so
each imports as a plain axis empty and re-exports byte for byte.

They are not in the Add ▸ 4DS menu — none is something you build, they are
things a file may already contain. To make one, add an empty and pick the type
from Frame Type, where they sit beside Target with the same plain-axes display.

The **Shadow** frame there is not the `.6ds` shadow beside the model. Those are
a separate file, marked with *Shadow Piece* on a mesh; this is a frame type
inside the model that happens to share the word. The panel says so on both.

**A dummy's box has drag handles.** A dummy is a box and nothing else, and
until now the only way to change one was typing six numbers. Select one and six
handles appear, one per face; drag a face and the box follows. The numbers stay
in the panel, because typing an exact figure is still the right way to enter an
exact figure — this is for the times you are eyeing it against the model.

They work on the 146 dummies an empty cannot draw as well as the 18996 it can,
which is the reason for doing it this way rather than with the empty's own size.
Sizing the empty was never an option: 8847 of the game's 19142 dummies carry a
frame scale that is written to the file, so borrowing the object's scale for
display would have corrupted nearly half of them.

Each handle sits in the middle of the face it moves, and the two halves of that
are kept apart on purpose. Where it is *drawn* comes from the face center, which
moves whenever any face moves, so it is recomputed on the way to every draw —
except for the handle currently being dragged, which Blender is already moving
and which jumps if the matrix under it is rewritten. What it *reports* is
measured along its own axis from the object's origin, the one point in a dummy
no drag can shift, so dragging one face never changes the number another handle
is working from.

The handle is drawn and dragged by the addon rather than by one of Blender's
stock gizmos. The stock arrow shows where a drag began as feedback, which on a
handle already sitting on the thing it moves reads as a second handle stuck
behind the first until you let go — and no flag turns that off. Owning the draw
means one handle is drawn, once, wherever the face currently is. The drag is
ours too: the handle's axis is projected to the screen and the pointer's travel
measured along it, so it feels the same whichever way the view is turned, with
the usual precise and snap modifiers.

Each handle still drags a real property rather than a pair of callbacks —
`ls3d_dummy_face_0` through `5`, computed from the box the same way every flag
checkbox in this addon is computed from its flag word, so nothing extra is
stored.

They appear only while the dummy is both active and selected — and only on a
**cube** empty, which is the shape a dummy is. An empty stands for a frame by
its shape and always did, but every shape the list did not name used to fall
through to Dummy, which handed box handles to helper empties and wrote them into
the model as boxes nobody asked for. These shapes name a frame:

| Display As | stands for |
|---|---|
| Cube | Dummy |
| Sphere | Lens Flare |
| Arrows | Projector |
| Cone | Light |
| Plain Axes | Target, or a frame that stores only its transform |

An empty of any other shape is a helper somebody put in the scene. It gets no
4DS settings, its panel says so and what the shapes mean, and the export leaves
it out with a word about why rather than guessing it into the file.

An empty you made yourself and set to Dummy has no box stored. The handles fall
back to the size it is drawn at rather than collapsing onto the origin, and the
first drag turns that into a real box.

Nothing is stored. The handles are placed from the object's transform and its
box on the way to every draw, and dragging writes straight into the box the
export reads. A face pushed past its opposite stops just short, since an
inside-out box is not something the format can express.

The outline is blue at rest and **red** once the dummy is selected — its own
color rather than the theme's orange, because it sits next to the empty's cube
which Blender is already turning orange, and one orange and one red separate
better than two oranges a shade apart.

A box that is not centered on its empty also has a **dashed line** from the
middle of the box to the empty, in the box's color. It works like Blender's own
parent lines: it follows the viewport's **Relationship Lines** overlay toggle,
and its dashes stay the same size on screen at any zoom. A box centered on its
empty gets no line, because it already surrounds the empty.

**Smaller things.** The Add ▸ 4DS menu builds a **billboard** — an upright quad
turning freely around the upright axis, which is what 199 of the game's 293
billboards do, a **projector**, and a **light** — a spot pointing down at the
floor with the engine's own starting figures, which is a light that already
lights something rather than one you have to switch on first. Selected Objects
Only now works for every format. A model
lands in a collection named after it. Exporting a model can write its animation
beside it, and exporting an animation its movement, each under the name you
type. Console lines are tagged `[MBT/4DS]`, `[MBT/5DS]` and so on. The
sidebar can put the scene on the game's 25 fps, and can stop every target frame
aiming while you animate.

### The export has no fallback values

**Whatever is set is what gets written.** The export used to read its values
through helpers that took a default — `_int_prop(obj, "ls3d_frame_type",
FRAME_VISUAL)`, `getattr(obj, "bbox_min", (0, 0, 0))`, and a dozen more — and
those defaults were unreachable in normal use, because every one of those is a
registered property that always carries a value. So they were dead code that
could only ever fire when something was genuinely wrong, and then hid it behind
a number nobody had chosen.

They are gone. A value that cannot be read now stops the export with a message
naming the object and the property, and **nothing is written**. The same goes at
the format layer: a visual frame with no visual type, a projector with no
payload, an emitor with no particle mesh — each used to be written as a blank
stand-in, producing a file that loads and is wrong, which is worse than one that
was never written. Each now refuses.

Defaults still live in the two places that mean something: the registered
property's own default, and what the Add ▸ 4DS menu seeds onto a newly created
object. Those say what a *new* object starts as. They are not substitutions at
export time, and there is a test that fails if one creeps back into the export
path.

The line between an error and a warning is whether the file would be wrong. A
value that would corrupt what the game reads — a projector naming material 0, an
occluder past the silhouette walk's face ceiling, a LOD claiming 65535 vertices —
is an error and stops the export. A value that produces a perfectly valid file
which merely behaves badly, like a mirror with a reflection range of zero, is a
warning.

### Mirrors

**A mirror from the Add menu can now actually reflect.** Its reflection range
started at zero, and zero is not "no limit" — the game measures the camera
against it and gives up at any distance past it, so the mirror drew its flat
tint and nothing else, silently. New mirrors start at **100**, which is the
engine's own default and what all nine mirrors the game ships store; a mirror
somebody zeroes by hand is now called out on export.

The nine are worth knowing about, because eight of them are `kaluz*.4ds` —
*kaluž*, puddle. The reflective-surface visual ships almost entirely as wet
ground; only `zrcadlo.4ds` is a mirror. All nine carry the renderable-object bit
clear, all nine are flat along local Y, and every one stores its bound's sphere
as the box's own enclosing sphere.

**The view box is four fields on the mirror, edited like a dummy's box.** The
file has no min/max pair for it: it keeps a matrix that carries a unit cube onto
the box, made of the box's center and three axes, each running from the center
to the middle of a face. That box is the volume deciding what appears in the
reflection. Four of the game's nine are cubic, five are not, eight are rotated,
and some are slightly skewed, which a location, rotation and scale cannot hold.
So the mirror's 4DS panel shows those four as they are, under **View Box**:
**Center**, **X Axis**, **Y Axis** and **Z Axis**, in the mirror's own space,
with the box's size below them. They are the file's own numbers, so a view box
comes back to the bit however often it goes round.

The box is outlined in the viewport in pink, brighter while the mirror is
selected, and a selected mirror gets a handle on the middle of each face, the
same handles a dummy has: dragging one moves that face and leaves the opposite
one where it was. **Fit View Box** sets a cube standing on the mirror's surface
and reaching out in front of it, which is also what Add ▸ 4DS ▸ Mirror starts
with. A mirror whose view box has an axis of no length — one you set to Mirror
by hand and never gave a box — is refused on export rather than written with a
box nobody chose; one sitting entirely behind the surface is warned about.

What survives a trip through Blender: all of it. Every one of the nine comes
back identical to the file it came from, the save date aside, and stays that way
through any number of re-exports.

### Transparent materials

Four switches decide how a surface reaches the screen, they override one
another, and an emission color joins in without being one of them. The preview
used to read them as independent settings and got several combinations wrong.
It now follows the same order the game does — **Alpha Additive**, then **Use
Alpha Texture** (or any opacity below 1), then **Color Key** — and the first
one set decides.

- **Additive surfaces are now added, not faded.** The game adds them to what is
  already on screen, so their dark areas disappear on their own and their
  bright areas brighten the background. Blender's alpha does the opposite: it
  *darkens* the background in proportion to how transparent the surface is.
  The preview now uses a genuinely additive shader for these.
- **An emission color turns a faded surface additive.** That is not obvious
  from the panel and it is not rare: **784 of the game's 1368** faded materials
  carry one, and they are its fires, sparks, muzzle flashes, explosions and
  lamp halos. Every one of them previewed as a darkening fade before.
- **The color-key cut-off is an eighth, not a half.** A keyed pixel is drawn
  whole or thrown away, on a test it has to beat by 32 of 255 — so keyed edges
  are no longer eaten into. The test also lives in the node graph now rather
  than in a Blender material setting, so it behaves the same in every version.
- **Each kind of transparency is read from where the game reads it.** A
  separate alpha texture is folded to one channel by weight (0.30, 0.60, 0.10)
  rather than averaged; a truecolor texture's transparency comes from its own
  alpha channel; and an additive material has no transparency source at all,
  because its texture is loaded without one.
- **The color key follows the texture it is read from.** It is palette entry 0
  of the diffuse image, not something you type, but it was sampled only while
  still black — so the first texture a material ever had went on deciding what
  was see-through even after the texture was swapped. It is now re-read
  whenever the material changes, including when the file itself is rewritten on
  disk. A file that cannot be read keeps the last key rather than wiping it,
  since an import samples it while the texture folder is set.
- **The color key is matched a byte at a time.** A pixel is the key when each
  channel is nearer the key's byte than any other, worked out from the byte
  the key was read from, so a black key no longer swallows every dark pixel
  near it and a middle tone still catches all of its own. A texture with no palette is keyed on its blue channel alone, which is
  what the game ends up doing to those.
- **The panel says where the switches landed** — *Draws as: Additive*, and why,
  when an emission color or a low opacity is what decided it.
- **A color-keyed material with Diffuse MipMap is blended, not cut.** The game
  loads a keyed texture with a whole alpha channel rather than a single bit
  when that bit is on, and draws it blended, throwing away only what is fully
  transparent - so its keyed edges soften with distance instead of being cut
  the same way at every size. The preview called these *Color Keyed* and built
  the cut-out graph. They now read *Color Keyed, Blended*, say why, take their
  alpha from the key mask as it stands rather than through the cut-off test,
  and draw blended; and because an emission color turns any blended surface
  additive, a keyed and mipmapped material carrying one now previews as
  *Additive*. 1,577 of the game's 4,609 color-keyed materials are in this
  case, 7 of them emissive; the other 3,032 are cut as before.

**Opacity belongs to the mesh, not to the material.** This is the one material
setting the game does not read per material. It puts a single alpha on every
vertex of a mesh and takes it from whichever material leads that mesh's draw
order — and it moves any material asking for transparency behind the ones that
do not, so the leader is the *last solid* material on the mesh. A solid
material has an opacity of 1 by definition, so **one solid material anywhere on
a mesh makes every Opacity on that mesh stop meaning anything.**

Only 25 of the game's 19831 materials set an opacity at all and 8 of those uses
are overridden this way, but two of the eight are set to 0 — so reading the
number at face value made the whisky glass in `FMVwhiskymrph` disappear where
the game draws it frosted. The preview now applies the rule, and the panel says
*Drawn at 1.00, not 0.00* when a material's own number is not what reaches the
screen.

### Environment maps

- **The blend mode is a number, not three bits.** The five modes — blend by
  amount, blend by the surface's own alpha, multiply, multiply doubled, and add
  — were being read as independent flags, which turned *multiply* and *multiply
  doubled* into something else. 58 of the game's materials use those two. The
  mapping mode is read the same way, all six values of it.
- **The reflection arrives unlit.** The game lights the diffuse texture and
  only then combines the environment map with the result, so in the two blended
  modes and in add the reflection comes through whole however dark the surface
  under it is. That is what makes a glass read as frosted rather than as a dim
  gray tube, and it is why the game's env maps are nearly black with a few
  bright streaks — they are meant to be added, not averaged. Only the two
  multiply modes light the reflection, and those are handled separately.
- **The reflection is wrapped the game's way.** Blender's ready-made spherical
  projection used to stand in for it; that is a lat-long mapping and the game's
  is not, so on a map that is 97% black the highlight did not soften, it landed
  somewhere else on the model. The projection is now built from the game's own
  arithmetic and agrees with it to within 6e-8 across both reflect modes and
  every repeat count, including the axis swap and the flipped V. The repeat
  count widens the reflection rather than tiling it, as it does in the game.
- **An untextured material shows its own diffuse color** instead of black. The
  game paints with that color exactly when there is no texture to paint with,
  or when *Vertex Colors* asks for both. 306 materials previewed black before.

### Light the surface makes itself

**Emission multiplies the texture; it is not laid over it.** The game adds the
emission color to the light landing on a surface and multiplies the texture by
the sum, so a lit window keeps its panes instead of turning into a gray
rectangle. 93 of the game's materials carry an emission color on a surface it
still lights, and every one of them was being washed flat.

There is no doubling anywhere any more, either. The game does double the
surface on its way to the screen — but it measures the light on a half-scale to
begin with, and the two cancel exactly. What reaches the screen is the texture
times the light on it, so an emission of 0.498 shows the texture at about half
brightness rather than at full. The additive branch was doubling it and is not
now.

### Animated textures

**How the frames are named.** The game numbers them by overwriting the **two
characters immediately before the extension** with a count from `00` upward, so
the name stored in the file is already frame `00` — nothing is appended to it.
`2FIRESM00.BMP` runs `2FIRESM00` … `2FIRESM15`; `FLAME1_0000.BMP` runs
`FLAME1_0000` … `FLAME1_0004`, because only the last two digits are the counter
and the `00` before them is part of the name. Two digits is the whole width, so
100 frames is the ceiling and the export says so if a material asks for more. A
name shorter than six characters has nowhere to put the number at all.

The panel now spells the range out — *Diffuse: 2firesm00.bmp to
2firesm15.bmp* — rather than leaving the rule to be guessed. The earlier flag
descriptions claimed frames were "numbered with a 01 suffix"; that was wrong,
and `01` is simply the second frame.

*Anim Alpha* does nothing on its own. It steps the alpha texture through its
own frames alongside the diffuse one; without it the alpha holds the single
name it carries while the diffuse animates under it. *Anim Diffuse* is what
makes a material animate at all.

**The block was being misread, and re-exporting destroyed it.** It is six
fields — a frame count for the diffuse and one for the environment texture, a
byte each, then a wrap-to frame and a frame time for each. The addon read the
first four bytes as one number, which swallowed the environment count and half
the wrap-to frame: `vodapristavm.4ds` showed **276 frames** for a 20-frame water
animation, and because the panel field caps at 99, exporting it wrote **99**
back and lost the rest. Six of the game's models were affected, `explosion.4ds`
among them — it wraps back to frame 10, and that 10 was being thrown away.

All six fields now have their own control, and all 18 animated materials in the
game carry through Blender and back unchanged.

**How much of this the game actually uses**, measured across all 3237 shipping
`.4ds` files rather than assumed from the engine's field list:

| Field | In the game |
| --- | --- |
| Diffuse frame count | 3 to 90, and the frame files on disk match it |
| Diffuse frame time | 6 ms to 180 ms |
| Loop back to | 0 everywhere except `explosion.4ds`, which uses 10 |
| Random frame (−1) | **never used**, though the game implements it |
| Env frame count | 0, or 1 on two materials — **never the 2 it would need** |
| Env frame time | **always 0** |
| Env loop back to | **always 0** |

So the environment texture has a complete animation channel — its own frame
list, timing and stepping code, called from the render paths — and **nothing in
the game switches it on**: a frame count under 2 or a frame time of 0 is
exactly what the game reads as "not animated", and every shipping material is
in that state. Those three controls are carried so a file that does set them
survives a round trip, and the panel keeps them hidden until one does. Only the
diffuse frame count is corroborated by anything outside the engine's own field
layout; the environment fields are read where the game's fields sit, and no
shipping data exercises them.

### Imports got faster

Every material property rebuilds the preview graph as it is set, which is what
keeps the panel live under your hands — but an import sets a dozen of them in a
row on each of several hundred materials, building the graph a dozen times over
to throw eleven away. It is now held until the material is filled in and built
once. The largest scene in the game, `FREERIDE` at 6.5 MB and 694 materials,
imports in **10 seconds where it used to take 34**.

### What changed in 0.7.0

- **Both inverse-bind matrices carry joint scale.** Skinning multiplies the
  bind by the joint's *scaled* local matrix, so on files shipped with the game
  `jointWorld @ bind` is exactly
  identity, and scale propagates down the whole chain — every bone in
  `TommyHIGH` carries 1/1.0506 in its bind because the root joint `back1` is
  scaled, even though the other joints sit at 1.0. Building the bind from the
  unscaled rest matrix left that uncanceled. 197 models have scaled joints.
- **Joint scale stays an editable value with no visual effect**, as in 0.6.2.
  It drives only how large the joint is drawn. Posing the bone by it would
  deform the skinned mesh, which is *not* what the engine shows: the binds
  cancel the authored scale exactly, so the game draws the mesh undeformed at
  rest.

- **Objects parented to a bone are placed correctly again.** Bones sit at their
  scale-free rest position so the skeleton reads cleanly, but the engine chains
  T@R@S including each joint's `ls3d_joint_scale`. The bone-parent matrix has to
  compensate; without that term hands, eyes and weapon dummies on the
  high-detail characters landed 3.4-4.4 cm out.
- **The weight auto-fixes are back**, in `4ds/validation.py`. Both switches
  ("Fix >2 Joint Influences" and "Fix Non-Parent-Child Weights") work as
  before: without them a violating vertex is a hard export error naming the
  joints involved; with them the weights are repaired and reported as
  warnings. They moved out of the addon preferences into the 4DS export
  dialog, which is where they take effect.
- **The alpha-texture presence rule now matches the engine.** Whether a
  separate alpha-texture name follows the diffuse one was decided by the "Alpha
  Enable" bit; the game decides it from the alpha-texture bit *and* the
  absence of the vetoing bits, so a color-keyed, truecolor or alpha-tested
  material has no such name. The two rules agree on all 44655 shipping
  materials, but they part on a hand-authored one - and disagreeing with the
  engine about whether a field is there desyncs everything after it.
- **A sector check that could never fire now can.** "Does not enclose a volume"
  sat behind the closed-mesh test, which always caught the same meshes first.
  The order is swapped, so a flat plane is told what is actually wrong with it.
- **The color-key warning no longer repeats when no texture folder is set.**
  With no folder configured the import already says so once; adding a second
  warning per color-keyed material buried the ones that mattered. It still
  fires when a folder *is* set and the file is genuinely missing.
- **Updating the addon without restarting Blender no longer half-loads it.**
  Installing over a running Blender re-imports `__init__.py` but leaves the
  submodules already in `sys.modules` untouched, so the new entry point could
  call into the previous version's code - which showed up as an AttributeError
  for a method the source plainly had. The package now reloads its own
  submodules on import, so an in-place update takes effect straight away.
- **LOD switch distance is shown in meters.** The file stores the *square* of
  the distance: it is compared against a squared camera distance with no
  square root taken, and the
  shipping values only make sense that way - Sam's eyes hold 0.484 (0.70 m) and
  the zeppelin in the harbor scene holds 789823 (889 m, not 790 km). The panel
  now shows the meter figure; the raw squared value sits behind **Show Raw Flag
  Values**. What gets written is unchanged, so files still round-trip untouched.
- **A mirror's view box follows the empty's display size.** The engine reads
  the stored matrix's basis vectors as the box's half-extents, and Blender draws
  a cube empty at scale x display size - so resizing an empty the obvious way
  had no effect on the export. The display size is now folded in, and imported
  view boxes are pinned to 1.0 so nothing shifts.
- **The structural checks from the original addon are back.** Sectors and
  occluders are tested for being closed volumes, mirrors for having exactly one
  properly formed view box, armatures for their skinned mesh,
  skinned meshes for geometry/weights/shape keys, morph objects for groups with
  a resolvable basis key, and portals for the engine's eight-corner limit
  (a fixed slot of eight - a ninth corner does not fit).

  Convexity is a **warning**, not an error, and that is deliberate. Measuring
  all 1129 sector hulls the game ships: every one is closed, but their convexity
  is only approximate - 45 are off by over a millimeter, five by over a
  centimeter, and one in MISE20-GALERY by 114 mm. Treating that as an error
  would make the addon unable to re-export Mafia's own missions, so the
  tolerance sits above the worst shipping hull and the message advises rather
  than blocks.
- **Import and export show progress.** Blender's progress bar fills as the run
  proceeds and the cursor turns busy, the way the original single-file addon
  did. The bar is driven through the report object, so the stages it already
  prints - Materials, Frames, Post-processing, Writing - are the same ones it
  advances through. The cursor is restored even when a run fails, which
  previously left Blender stuck showing the busy pointer, and the result popup
  now appears after the bar has closed rather than over the top of it.
- **The flag panels name what the flags actually do.** Every "Unknown N" was
  resolved against what the game actually does with each bit, and given its
  own terminology, with a one-line note on what it changes. Culling keeps
  *Enable* and *Position Locked*; Rendering keeps *Flat Lighting*,
  *Managed LOD* and *World Space Geometry*; Logic keeps *Z-Bias*,
  *Mesh Object*, *No Two-Sided Collision* and *No Fog*, plus two dropdowns.

  Shadows and projections stay four checkboxes, and the original names were
  right: the first bit of each pair decides whether the object is collected as
  a receiver at all, the second is handed to the clipper and decides whether
  face groups using color key, alpha blending or alpha test are clipped in
  too. *Shadows on Diffuse* / *Shadows on Alpha* describes that exactly.

  Three material checkbox groups did become fields, because they really are
  numbers packed into the flag word - *Env Blend Type*,
  *Env Mapping Mode* and *Env Tiling* are a 3-bit, a 3-bit and an 8-bit field
  packed into the material word. Four material controls that could not be
  edited at all appeared: *Force Truecolor*, *No Compression*, *No Presaved
  Texture* and *Texture Manager*.

  Some old names were actively wrong. "Cast Shadow" was the engine's rotation
  cache bit; "Mirrorable" is *Mesh Object*, the tag that lets the engine read
  the mesh and walk its LOD chain; "Hide Mesh" is *World Space Geometry*, which makes an object ignore
  its own transform - hence the reports of meshes jumping to the origin.

  Nothing is hidden away permanently:

  - A bit the loaded model actually has set is **always** listed, under
    *Engine Internals*, whatever the preferences say.
  - **Show Engine-Reserved Flags** (off by default) lists the rest of them.
  - **Show Raw Flag Values** (on by default) keeps the numeric field above each
    checkbox grid.

  Both live under **Flag Panels** in the addon preferences and take effect
  immediately. Every bit of all three frame flag bytes and all 32 bits of the
  material word have a control, and the raw field and the checkboxes are one
  value seen two ways: type a number and the toggles follow, click a toggle and
  the number follows. Flags are stored and written back byte for byte
  regardless of what the panel shows, so a file using a bit this addon has
  never heard of still round-trips untouched.

- **Instanced objects are identifiable in the object panel.** An
  "Instancing" block reads `COPY` with the master it borrows from, or `MASTER`
  with the copies that borrow from it. *(Later builds also name linked
  duplicates made by hand, and dropped the viewport tags and faded object color
  this release introduced — see "Instancing is shown in the object panel only"
  above.)*
- **A truecolor texture no longer warns about its color key.** The key is
  palette entry 0, so a 24-bit BMP has none to read — 8 of the 336 color-keyed
  textures in the game are truecolor. Those now take the documented black
  fallback silently, as in 0.6.2, and the warning is reserved for a texture that
  could not be opened at all.
- **The group's basis is pinned to the first slot.** The reorder arrows let it
  be moved down, and let slot 1 move up into it, which silently rebased the
  group while the list still labeled row 0 "Basis". Both are refused now with
  an explanation, the arrows gray out when they would be, and "Apply as Basis"
  remains the way to change which target is the basis — it rebases the shape
  key geometry as well.
- **The morph target list shows each target's value slider again**, with the
  group's own basis row labeled instead of given a slider.

### Crashes

- **Zero-area faces no longer take Blender down.** A triangle naming the same
  vertex twice draws nothing, but a mesh carrying enough of them makes
  `normals_split_custom_set` segfault rather than raise — `Line23` in
  `00MENU/scene.4ds` has 228 and killed the process outright. They are dropped
  on import with a warning; 1204 exist across 121 shipping files.
- **The `depsgraph_update_post` handler is gone.** It walked every object and
  every pose bone on *every* depsgraph update, and writing IDs from that
  callback can fire while Blender is tearing a scene down.

### Data loss on export

- **A failed export no longer destroys the file it was writing to.** The
  exporter used to open the target with `"wb"` before validating anything, so
  any validation failure truncated an existing model while the UI reported only
  that the export had failed. The document is now built and validated entirely
  in memory, then written to a temporary file and moved into place.
- **A skinned mesh bound by an Armature modifier is no longer dropped.** Frame
  ordering was rebuilt from armature/mesh pairs matched by *parenting* only, and
  anything unmatched fell out of the export with no error, producing a valid
  file with the character missing.
- **Morph targets get their own normals.** The format stores a normal per
  vertex *per target*; the exporter wrote the basis mesh normal for every target.
  389 vertices in `SamHIGH.4ds` and 307 in `vodapristavm.4ds` carry genuinely
  different normals per target. Each target's normals are now worked out from
  its own shape on export.
- **Portal planes are computed from the geometry.** The recompute branch was
  unreachable (`hasattr` on a registered property is always true), so a portal
  authored from scratch shipped a zero-length plane normal and a portal that had
  been moved shipped the plane from its old position.
- **Materials used only on a LOD mesh are exported.** The material table was
  collected before LODs were discovered and from a different object set, so
  those faces silently exported with material id 0.
- **A billboard's rotation axis is written in both modes.** The axis and the
  "single axis" lock are independent fields. The old rule forced the axis to 0
  whenever the lock was clear, which silently rewrote **127 billboard frames
  across 28 files** — axis 2 with the lock clear is the most common combination
  in the game. The axis is now always shown in the panel too.
- **Texture names come from the file name, not the datablock name.** Blender
  appends `.001` on a name collision, and `WOOD.BMP.001` is not a texture the
  engine can find.
- **`"_lod"` is matched as a suffix.** An object called `car_lodge` was treated
  as a LOD of `car` and excluded from the export.
- **Sector and occluder vertex counts are checked** against the format's 16-bit
  index limit instead of raising `struct.error` part-way through writing.
- **Sector geometry is written in world space with an identity frame transform.**
  Sector vertices are absolute, not relative to the frame: the single sector in
  the game that has a parent (`sector Box01kr` in `MISE04-MESTO`, parented to a
  frame translated by ~(202, 28, 1363)) stores coordinates that ignore that
  translation completely. The importer cancels a sector's parent transform to
  match, so the hierarchy link still round-trips without displacing the mesh.
- **The two kinds of empty frame are kept apart.** A frame with no LOD blocks at
  all (four in `MISE13-ZRADCE`) and a frame with one LOD holding no vertices
  (five in `FREERIDE`) look identical once imported, so the importer records
  which it was. Treating them as one cost `FREERIDE` five LODs.
- **Textures whose flag is off are reported.** The format only stores a texture
  name when its flag is set; assigning an image and leaving the flag clear used
  to drop it silently.
- **Over-long names and user properties warn** instead of being cut at 255 bytes
  in silence.

### Data loss on import

- **Instanced frames get their geometry.** A frame that reuses another frame's
  mesh was left empty and nothing resolved it. Verified against the game data:
  0.6.2 imported **all 5** instances in `vjezdtov1.4ds` and **all 11** in
  `MISE08-KOSTEL/scene.4ds` as empty meshes. They now share the source mesh
  datablock, land at the right position, and re-export as instance references
  pointing at the same geometry.
- **Instanced billboards parse.** The billboard block belongs to the frame, not
  the mesh, and is written even when the frame is an instance. Skipping it left
  the reader five bytes short and turned the rest of the file into noise —
  `MISE16-LETISTE/scene.4ds` failed this way, and the garbage happened to
  decode as a `FRAME_LIGHT`, which made it look like an unsupported feature
  rather than a parsing bug.
- **Zero-area faces no longer crash Blender.** A triangle naming the same vertex
  twice draws nothing, but a mesh carrying enough of them makes
  `normals_split_custom_set` take the whole process down rather than raise —
  `Line23` in `00MENU/scene.4ds` has 228 of them. They are dropped on import
  with a warning; 1155 exist across 91 shipping files.
- **Import failures are reported as failures.** `import_file` caught every
  exception, logged it, and returned normally, so the operator reported
  `Import OK` over a half-loaded scene.
- **Instanced frames get their geometry.** A frame that reuses another frame's
  mesh was left with an empty mesh and nothing ever resolved it — 173 objects in
  the shipping game data are instances.
- **Unsupported frame and visual types abort with a message.** The format has no
  per-frame length field, so an unrecognized payload cannot be skipped; the
  reader used to continue and reinterpret the rest of the file as noise. (Sound
  and Area frames are now the exception: their length is known — three bytes —
  so they are skipped rather than refused.)
- **Double-sided faces survive.** Faces sharing three vertices with opposite
  winding are real geometry (25,705 of them across 614 shipping models) and are
  no longer filtered out.
- **Material assignment no longer silently reverts to slot 0.** Faces are only
  dropped when they index a vertex that does not exist, so the per-face material
  list always lines up with what Blender built.
- **Bone renames are followed.** Vertex groups were named from the file while
  the bone might have been renamed by Blender on collision or length, leaving
  the armature modifier with nothing to bind.
- **A parent that failed to import is reported** rather than silently reparenting
  the child to the scene root.

### Operators

- **`Apply as Basis` only touches its own group.** It shifted every shape key on
  the object, so rebasing one region displaced every other region's targets by
  that region's delta — and it measured the delta against Blender's reference
  key rather than the group's own basis.
- **Reordering a morph target no longer reorders `key_blocks`.** Moving a shape
  key to slot 0 in Blender changes which key is the mesh's reference basis.
- **`Transfer Morph` restores UI state** if it fails part-way.

### Stability and performance

- The `depsgraph_update_post` handler that walked every object and every pose
  bone on **every** depsgraph update is gone. Besides the cost, writing IDs from
  that callback can fire while Blender is tearing a scene down, which crashes
  the process. The property it seeded is set where it is actually needed.
- Vertex splitting compares corner normals with a tolerance instead of exactly.
  Blender renormalizes custom split normals when they are applied, and splitting
  on that drift grew meshes a few percent on every import/export cycle.

## Joint scale and the skinned mesh

Joint scale now behaves as the game does, in three respects.

**At rest the mesh is undeformed, and that is correct.** Skinning multiplies
the bind by the joint's own matrix, and measured on files shipped with the game
`jointWorld @ bind` comes out exactly identity for every joint — including
non-uniform ones like `[0.9465, 1.0566, 1.0]`. The bind cancels the authored
scale, so the rendered rest pose equals the stored mesh.

**Editing a joint's scale moves nothing.** `ls3d_joint_scale` is an editable
value. In Blender it drives only how large the joint is drawn, through the pose
bone's `custom_shape_scale_xyz`. The bones already sit where the whole chain,
scale included, puts the joints. So the value decides how the export splits
each position between the joint's own offset and the scale above it: what is
drawn is what the game shows. The binds cancel the authored scale exactly, so
the game draws the mesh undeformed at rest whatever the joints are scaled to.
The value is written back to the file, and governs the transform chain that
places bone-parented objects.

**Both inverse-bind matrices now include joint scale.** This was a real export
bug affecting the 197 models with scaled joints. The format stores two
matrix-shaped things per joint and only one of them is the bind:

* the **skin block's** per-bone-group matrix *is* the bind, and must invert the
  joint world *including* scale. It was being built from the unscaled rest
  matrix. Scale propagates down the chain, so this was wrong even for bones at
  1.0 — every bone in `TommyHIGH` carries `1/1.0506` because the root joint
  `back1` is scaled.
* the **FRAME_JOINT payload's** 16 floats are *not* a bind. They are the
  joint's **area of influence**: a box in the joint's own space that the skin
  weights were made from when the model was exported. Its third axis runs
  along the bone; a vertex inside the box is shared between the joint and its
  parent by where it sits along that axis, from none of it the joint's at the
  back face to all of it at the front, and the vertices past the front face
  belong to the joint alone. All 83,347 blended vertices in the game's 289
  skinned models sit inside their joint's box with exactly that weight, to
  within 0.00003. The game itself loads the numbers and copies them when a
  joint is duplicated, and nothing reads them for drawing, skinning or bounds:
  it plays the weights, not the box. In all 5223 joints the box has three
  square axes, each its own length (from 3 mm to 53 cm, around 9 cm
  typically), and none is the game's own default of identity × 0.01. In
  Blender each is kept on its joint, as the file's own sixteen numbers, and
  drawn over the bone it belongs to; nothing of the box goes through a
  Blender transform, so every digit of it comes back exactly as it went in.
  `LS3D-4DS-Joint-Viewer.html`, next to
  the field manual, opens any model in the browser and draws these boxes on
  it, with the picked joint's weights on its vertices, beside the skin's group
  boxes the game does use for the mesh's bounds.

## Joint scale

The transform chain is pinned down rather than inferred, so joint scale is
exact rather than merely plausible:

* a frame's local matrix is the rotation with each basis row multiplied by the
  matching scale component and the translation left alone — that is
  `Matrix.LocRotScale(location, rotation, scale)`.
* a frame composes against its parent using row vectors and basis rows, which
  transposes to `parent_world @ local` in Blender.
* the skinning chain walks a joint's parents and stops at the first visual,
  sector or model frame, so it is relative to the mesh frame — and it uses the
  scaled local matrix, meaning joint scale applies to skinning just as it does
  to parenting.
* the averaged `(sx+sy+sz)/3` world scale is only used by gameplay code, never
  by rendering or skinning, so it has no bearing here.

Measured against that formula on `TommyHIGH`, every bone-parented object now
matches to **0.00000**. The regression check asserts exactly that, muting the
Track To constraints first: target frames are mirrored into Blender as
constraints that deliberately pose the bone they act on, and TommyHIGH's neck
tracks `targetN`, which swings the eyes and head mesh a few millimeters off the
rest pose. That is the feature working, not an error.

## Known behavior

A mesh made in Blender that makes a surface double-sided by storing a second
copy of each triangle wound the other way gains a few vertices on export.
Blender gives those two faces opposite corner normals, so the shared vertex has
to split. The result shades the back face correctly rather than lighting it as
if it faced front, so this is left as-is. An imported mesh carries the file's
normals on every corner and does not split.

A face whose three corners are not three distinct vertices - 63 of the game's
models carry 779 of them - is kept, not dropped. Blender cannot hold such a
face, so the import gives the repeat a copy of the vertex and the export merges
it back, writing the face as it came.

A copy is only merged while the two really are one vertex twice: as well as
sitting at the same spot with the same normal and UV, they must carry the same
weights and be put in the same place by every morph target. A corner weighted
to another joint, or lifted away by a target, parts from its twin the moment
the model moves - a face that covers no area at rest can open when a joint
turns or a target blends - so it is written as a vertex of its own. Without
that rule a hand-built mesh lost the weight or the target movement on such a
corner silently. The game's own models are unaffected: an imported copy is
given its twin's weights and targets, so it still merges, `Detektiv02` - the
one model that is skinned, morphed and holds such faces - included.

One model pays for the rule: `4old071` ends a mesh on a real vertex that looks
exactly like one of those copies - used once, sitting on an earlier corner of
its own face with the same normal and UV, and moving with it - and it is merged
too, so the model comes back with 408 of its 409 vertices and one already-flat
face written with a repeated corner. Nothing the game draws changes, in any
pose, and no other model of the 3,119 is in this position. Telling the two
apart would mean carrying import data into the export, which nothing here does.

What a re-export still changes, and why, is listed under *Re-exporting writes
back the values a model came with*.

## Format notes

`4ds/codec/material.py`, `visual.py`, `scene.py` and `document.py` carry the
byte-level layout of each block as comments; `5ds/codec.py`, `tck/codec.py`
and `6ds/codec.py` do the same for the other three formats. Two
details are worth repeating because they are easy to get wrong:

- **`MTL_ALPHA_ANIMATED` has no payload block**, despite the name. Nine shipping
  materials set that bit and none carry extra bytes; reading a block for it
  desynchronizes the rest of the file.
- **A geometry LOD cannot have 65535 vertices.** The count is a `uint16`, but
  `0xFFFF` is not a count there: it marks a LOD that keeps LOD 0's positions and
  normals and carries only its own UVs, two floats per vertex. Writing 65535
  real vertices produces a file the game reads as that shorthand and mangles, so
  the ceiling is 65534. Sector, occluder and mirror hulls are unaffected — they
  count in `uint32` and have no sentinel — so they keep the `uint16` index limit
  of 65535 instead.
- **An occluder's face count has a ceiling the format does not state.** The
  counts are both `uint32`, but the game builds an edge-adjacency table for the
  silhouette walk and holds each face index in it as a `uint16`. Past 65535
  faces the adjacency wraps and the walk follows the wrong neighbors, with
  nothing to say so. The export refuses it.
- **The two material texture strings are conditional** on `DIFFUSE_ENABLE` and
  on `ALPHATEX` with none of `ALPHA_ADDITIVE | COLORKEY | ALPHA_IN_TEX` set,
  with a single padding byte when neither is present. Reading them
  unconditionally happens to consume the same byte count on every shipping
  model, which is why a simpler parser appears to work. Those three vetoing
  bits are the ones whose materials carry their transparency in the diffuse
  texture, so no separate name follows.
