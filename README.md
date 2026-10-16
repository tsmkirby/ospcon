# OSP Connectivity

QGIS plugin for outside plant cabling. It links cables, panels, ducts, and devices from the attributes already on the map, traces a circuit back to an IDF, and splits a fiber cable where a splice enclosure is added.

It works with two project shapes:

- The OSP network template, which already has patch panels, splice tables, strand tables, and QGIS relations.
- The live NCL projects, which keep the same facts as text on the features and do not have those related tables.

QGIS 3.34 or newer is required. Developed against QGIS 3.44.

## Install

1. Copy or symlink the `osp_connectivity` folder into your QGIS profile plugins directory. On macOS that is `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`.
2. Restart QGIS if it was already open.
3. In **Plugins → Manage and Install Plugins**, enable **OSP Connectivity**.

The folder name has to stay `osp_connectivity`. QGIS loads the plugin from that package name.

## Toolbar

The **OSP Connectivity** toolbar has five actions. Results open in the **OSP Report** dock, which can copy the text, export it as CSV, or clear the map highlight.

| Action | What it does |
| --- | --- |
| Build associations | Reads the open project and fills links that the attributes already imply. |
| Associate selection | Builds one circuit from the features you have selected, then asks for the panel and port. |
| Trace selection | Follows the selected device, cable, or IDF and highlights the path. |
| Add fiber enclosure | Click a fiber cable. The cable is split and you choose which strands are spliced. |
| Set snap tolerance | Sets the distance used for duct chaining and enclosure clicks. |

### Build associations

Use this when the ends are already written on the features. For each cable, device, IDF, pole, and duct the plugin can recognize, it writes the matching link and leaves a note when an end cannot be resolved.

It never overwrites a field that already has a value, including placeholders such as `unk`. Duplicate cable ids are reported and are not merged. The first cable keeps the device, panel, and port when more than one cable claims them.

### Associate selection

Select the pieces of one path, then run **Associate selection**. A typical copper selection is an AP, its cable, the duct-bank segments it follows, the pole it is mounted on, and the IDF. The dialog asks for:

- Which end is the device and which end is the IDF. A and Z are labels, not a direction of signal. The dialog follows the pattern already used on that layer.
- The patch panel and port. On a template project the list comes from the panel layers. On an NCL project it lists panel names already used on cables that end at the selected IDF, and you can type a new one.
- For fiber, the strand numbers. Ports may be a single number or a range such as `1-2`.

The dialog will not save when no cable is selected, more than one device is selected, more than two IDFs are selected, copper and fiber are mixed, or the chosen port is already occupied.

Duct segments are ordered by chaining their endpoints. If they do not form one chain, reorder them in the dialog before saving. Ducts that have no segment id are stored by layer and feature id. The plugin does not invent segment ids.

### Trace selection

Select an AP, camera, jack, cable, IDF, or panel, then run **Trace selection**. The report lists the route in plain lines:

- Device
- Pole, when the device is mounted on one
- Cable id, cable type, and length
- Ducts, in order, joined by `>`
- IDF
- Panel and port
- Fiber strands, compressed into ranges, including splice hops

The map highlight uses orange for cables, blue for devices, green for IDFs, and magenta for splice enclosures. Duct banks are named in the report and are not highlighted. If the same cable id exists more than once, every copy is highlighted and the report says so.

Trace order is the selected device first, then a selected cable, then a selected IDF or panel. A panel-port trace walks the circuit back from that port.

### Add fiber enclosure

Choose **Add fiber enclosure**, then click a point on a fiber cable, away from either end.

- A mid-span click splits that cable into two segments. The original feature keeps its cable id and becomes the piece on the near side. The far piece receives the next id for that layer.
- Every strand is expressed through on the same strand number unless you say otherwise. Expressed strands are intact glass, not a fusion splice.
- In the splice dialog, mark strands that stay open and strands that are fusion spliced. A lateral cable whose end lands on the same point can take the fusion. Example: cable A strands 47–48 fused to cable C strands 1–2, with A strands 1–46 and 49–144 expressed to the new segment on the same numbers.
- Clicking where two through cables cross splits both and tells you the enclosure has four cable ends.
- A click on an endpoint does not split that cable. It is treated as a lateral that ends at the enclosure.
- Canceling the splice dialog rolls the split back.

Lengths are recalculated only when the layer’s map units are meters.

## Where the links are stored

The plugin uses the association tables the project already has. It creates a table only when that project has nowhere to put the record.

On the OSP template, splices, strands, terminations, routes, and enclosure membership stay in the existing GeoPackage tables. The first run can add a circuits table to the cable GeoPackage, and on a cable layer that has `cable_id` but no end field it can add `a_end_type`, `a_end_id`, `z_end_type`, `z_end_id`, and `strand_count`.

On an NCL project, the first **Build associations** or **Associate selection** creates `osp_links.gpkg` next to the `.qgz` and adds it to the project. Save the project before that run. The sidecar holds:

| Layer | Contents |
| --- | --- |
| OSP Fiber Strands | One row per strand |
| OSP Fiber Splices | Express, fusion, and open strand rows |
| OSP Cable Routes | Cable to duct order |
| OSP Circuits | Device, IDF, panel, port, and strand range |
| OSP Splice Enclosures | The enclosure point |
| OSP Enclosure Cables | Which cables enter the enclosure |

Feature edits are one undo step per layer, named **OSP Connectivity**. They stay in the layer edit buffer until you save the layer edits. Undo reverses the step until then. Each GeoPackage has its own undo stack.

## How layers are recognized

Roles come from the geometry and the field names, not from the layer name. That matters in the NCL projects, where several layers share a name. Cables are lines with `cable_id`, `strand_count`, `fiber_count`, or `media_type`. Ducts are the other lines, recognized from `segment_id` or a conduit-capacity field. Points are matched to access points, cameras, panels, splice enclosures, handholes, poles, and IDFs by their id fields.

Known field names are read as aliases. `a_end_location` and `a_end_id` are both cable ends. `patch_panel_id` and `patch_pane_id` are both the panel. `strand_count` and `fiber_count` are both the strand count, and a value such as `12F` is read as 12. New ids follow the prefix already used on that layer, so a Vibe fiber stays `vib.fib.0005` and a template cable stays `VIB-CB-0003`.

## Snap tolerance

**Set snap tolerance** stores the distance in QGIS settings under `osp_connectivity/tolerance`. The default is 1 map unit, which is 1 meter on a projected project such as EPSG:32618. On a project whose units are degrees and where the setting has never been changed, the default is 0.00002. Snapping transforms into the project CRS. The plugin does not reproject the source layers.

## What this version does not do

Dark-fiber inventory, a separate duct-fill report, a standing validation dock, printable circuit cards, and copper pair counts are not in this version. A waterpark-style fiber layer that has no A and Z ends is reported, and the plugin does not force it into a full circuit.
