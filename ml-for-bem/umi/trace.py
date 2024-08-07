import logging
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import List, Literal, Union

import geopandas as gpd
import numpy as np
import pandas as pd
import pyradiance as pr
import taichi as ti

try:
    from shapely import Polygon
except ImportError:
    from shapely.geometry import Polygon
# from shapely import Polygon
from ladybug.epw import EPW
from ladybug.wea import Wea

# ti.init(arch=ti.gpu, device_memory_fraction=0.7, kernel_profiler=True, debug=True)
# ti.init(arch=ti.cpu, kernel_profiler=True)
ti.init(arch=ti.gpu, device_memory_fraction=0.7, kernel_profiler=True, debug=True)

logging.basicConfig()
logger = logging.getLogger("Radiation Analysis")
logger.setLevel(logging.INFO)

# TODO: link n_azimuths between sky patch and tracer
# TODO: better parallelization of edge extraction
# TODO: deal with collinear edges which result in sensors inside neighboring building!
# TODO: azimuth angles should be starting at absolute not relative angles
# TODO: consider implementing computation skipping using lower sensors/elevation angles to infer upper values

# Declare a quantized datatype
uint1 = ti.types.quant.int(1, signed=False)


@ti.dataclass
class Node:
    height: float


@ti.dataclass
class Building:
    height: float
    n_floors: float
    archetype_ix: ti.i8

    edge_start_ix: int
    edge_end_ix: int
    edge_ct: int
    north_weight: float
    east_weight: float
    south_weight: float
    west_weight: float
    qualified_perim_length: float  # total length of perimeter ignoring edges below a certain threshold
    qualified_edge_weight_sum: float  # total weight of edges above a certain threshold


@ti.dataclass
class Edge:
    building_id: int

    start: ti.math.vec2
    end: ti.math.vec2
    slopevec: ti.math.vec2
    slope: float
    normal: ti.math.vec2
    normal_theta: float
    az_start_angle: float  # represents the AZIMUTHAL angle of incidence of the first ray
    orientation: ti.int8  # this ought to be ti.int2
    height: float  # TODO: This could be rounded to save memory, or stored with a parent building, e.g. uint16, or a quantized datatype e.g. uint10
    weight: float  # this represents the proportion of the total building perimeter which this edge represents
    qualified_length: float  # this represents the length of the edge, where edges below a certain size are set to 0.
    n_floors: ti.int8

    sensor_start_ix: int  # TODO: should these be forced to 64 bit?
    sensor_end_ix: int
    sensor_ct: int


@ti.dataclass
class XYSensor:
    hit_count: int
    loc: ti.math.vec2
    parent_edge_id: int

    xyz_sensor_start_ix: int  # TODO: should these be forced to 64 bit?
    xyz_sensor_ct: int


@ti.dataclass
class XYZSensor:
    height: float
    rad: float
    parent_sensor_id: int


@ti.dataclass
class Hit:
    loc_x_ix: ti.i16
    loc_y_ix: ti.i16
    height: float
    distance: float

    @ti.func
    def centroid(self) -> ti.math.vec2:
        return ti.Vector(
            [self.loc_x_ix + 0.5, self.loc_y_ix + 0.5]
        )  # TODO: assumes a bin spacing of 1m!


N_LOOPS_TO_UNROLL = 1


@ti.data_oriented
class Tracer:
    # TODO: make sure all class attrs are represented here
    node_width: float  # meters, determines scene xy discretization
    sensor_inset: float  # meters, how far the first/last sensors ust be from edge endpoint
    sensor_normal_offset: float  # meters, how far the sensors are moved off the parent surface
    sensor_spacing: float  # meters, gap between sensors
    f2f_height: float  # meters, floor-to-floor height
    max_ray_length: float  # meters, how far to trace each ray before giving up
    ray_step_size: float  # meters, how far to advance with each ray step
    steps_per_unroll_loop: int  # when performing gpu xyplane tracing to find all hits, this allows for non divergence
    n_azimuths: int  # number of azimuthal angles to use per sensor (from 0 to 180)
    n_elevations: int  # number of elevation angles to use (counting zero, excluding zenith)
    sky: "Sky"  # the sky to use for accumulating radiation in sensors

    n_ray_steps: int  # computed, max_ray_length / ray_step_size

    depth: int  # quadtree level count
    levels: List[ti.SNode]  # pointers to each 2x2 level of the quadtree
    nodes: ti.StructField  # spatially sparse data structure which discretizes the xy plane and which contains heights when an edge crosses through a node
    buildings: ti.StructField  # stores the semantic representation of a building
    edges: ti.StructField  # stores the semantic edge objects in a flattened list
    xy_sensors: ti.StructField  # stores the semantic xy plane sensor objects in a flattened list
    xyz_sensors: ti.StructField  # stores the semantic xyz plane sensor objects in a flattened list
    azimuths: ti.ScalarField  # stores precomputed azimuth OFFSETS - NB: these represent how far to rotate the edge's first ray in the xy-plane (that ray's azimuth is defined by az_start_angle for the edge)
    elevations: ti.ScalarField  # stores precomputed elevations - NB: these are
    xyz_sensor_views: ti.Field  # BitPacked Field which stores hits
    xyz_sensor_timesteps: ti.ScalarField  # n_sensors x 8760, stores accumulated irradiance for each time step

    gdf: gpd.GeoDataFrame
    height_col: str
    id_col: str
    archetype_col: str

    def __init__(
        self,
        sky: "Sky",
        gdf: gpd.GeoDataFrame,
        height_col: str,
        id_col: str,
        archetype_col: str,
        node_width: float = 1,
        sensor_inset: float = 0.5,
        sensor_normal_offset: float = 1.5,
        sensor_spacing: float = 1,
        f2f_height: float = 3,
        max_ray_length: float = 400.0,
        ray_step_size: float = 1.0,
        steps_per_unroll_loop: int = 100,
        convert_crs=False,
    ):
        self.sky = sky
        # TODO: add meter conversion, better crs validation/conversion
        # store the bin size in meters
        global N_LOOPS_TO_UNROLL
        assert (
            node_width == 1
        ), "Currently only supports dividing the space into 1 m node widths"
        self.node_width = node_width
        padding = 5 * node_width

        # store the sensor grid config
        self.sensor_inset = sensor_inset
        self.sensor_normal_offset = sensor_normal_offset
        self.sensor_spacing = sensor_spacing
        self.f2f_height = f2f_height
        self.max_ray_length = max_ray_length
        self.ray_step_size = ray_step_size
        self.n_ray_steps = int(self.max_ray_length / ray_step_size)
        self.steps_per_unroll_loop = steps_per_unroll_loop
        N_LOOPS_TO_UNROLL = int(
            np.ceil(self.n_ray_steps / self.steps_per_unroll_loop)
        )  # TODO: this should be a class property but it throws an error in the static unroll command in the kernel if so

        # Load the GDF
        # nb: assumes a flattened projection already holds.
        self.gdf: gpd.GeoDataFrame = gdf

        assert (
            len(self.gdf) < 2**16
        ), f"Currently, only {2**16-1} buildings are supported, but this GIS file has {len(self.gdf)} buildings."
        if convert_crs:
            logger.info("Converting crs...")
            # TODO: fix CRS conversions
            self.gdf = self.gdf.to_crs("EPSG:32633")
        self.height_col = height_col
        self.id_col = id_col
        self.archetype_col = archetype_col
        assert (
            height_col in self.gdf.columns
        ), f"The supplied height column '{height_col}' was not found in the GDF's columns: {self.gdf.columns}"
        assert (
            id_col in self.gdf.columns
        ), f"The supplied unique ID column '{id_col}' was not found in the GDF's columns: {self.gdf.columns}"
        assert (
            archetype_col in self.gdf.columns
        ), f"The supplied archetype column '{archetype_col}' was not found in the GDF's columns: {self.gdf.columns}"

        # compute number of floors
        self.gdf["N_FLOORS"] = np.ceil(self.gdf[height_col].values / self.f2f_height)

        # find the bbox
        x_low, y_low, x_high, y_high = self.gdf.geometry.total_bounds

        # relocate the gdf, with a little bit of wiggle room around the origin axes
        self.gdf.geometry = self.gdf.geometry.translate(
            -x_low + padding, -y_low + padding
        )

        base_gdf = self.gdf.copy()
        tile_ct = 0
        for i in range(tile_ct):
            for j in range(tile_ct):
                new_gdf = base_gdf.copy()
                new_gdf.geometry = new_gdf.geometry.translate(
                    ((x_high - x_low) + 2 * padding) * (i + 1),
                    ((y_high - y_low) + 2 * padding) * (j + 1),
                )
                self.gdf = pd.concat([self.gdf, new_gdf], axis=0, ignore_index=True)

        # compute the approx. number of sensors, ignoring sensor inset
        approx_n_sensors = int(
            np.floor(
                (
                    self.gdf.geometry.boundary.length
                    * self.gdf["N_FLOORS"]
                    / self.sensor_spacing
                ).sum()
            )
        )
        logger.info(f"Building Count: {len(self.gdf)}")
        logger.info(f"Approximate Sensor Count: {approx_n_sensors}")

        # find the new bbox
        x_low, y_low, x_high, y_high = self.gdf.geometry.total_bounds

        # check if length or width controls
        length = y_high + padding
        width = x_high + padding
        max_dim = length if length > width else width
        self.length = length
        self.width = width

        # compute how many bins are required in the longer axis
        min_nodes_required = int(np.ceil(max_dim / self.node_width))

        # compute how many binary divisions are needed in the longer axis
        self.depth = int(
            np.ceil(np.log2(min_nodes_required))
        )  # TODO: possibly not necessary if not using a quadtree
        assert (
            self.depth < 16
        ), f"Currently only supports quadtrees with a depth < 16, and a depth of {self.depth} is required for the {length,width} bbox"
        logger.info(f"QuadTree Depth: {self.depth}")

        # Create Scene Tree
        # TODO: explore performance and memory implications of using bitmasked instead
        self.levels = [ti.root.bitmasked(ti.ij, (2**self.depth, 2**self.depth))]
        # self.levels = []
        # root = ti.root.pointer(ti.ij, (2, 2))
        # self.levels.append(root)
        # for i in range(self.depth - 1):
        #     level = self.levels[-1].pointer(ti.ij, (2, 2))
        #     self.levels.append(level)

        # create a struct field and place it
        self.nodes = Node.field()
        self.tree_leaves.place(self.nodes)

        # Precompute azimuths
        logger.info(f"Initializing azimuths from provided sky...")
        assert self.sky.n_azimuths / 2 == int(self.sky.n_azimuths / 2)
        self.n_azimuths = int(self.sky.n_azimuths / 2)
        self.azimuths = ti.field(float, shape=self.n_azimuths)
        self.azimuth_inc = self.sky.azimuthal_aperture
        assert self.azimuth_inc == 2 * np.pi / (2 * self.n_azimuths)
        azimuths = np.arange(self.n_azimuths) * self.azimuth_inc
        self.azimuths.from_numpy(azimuths)

        # Building elevations
        logger.info(f"Initializing elevations from provided Sky...")
        self.n_elevations = self.sky.n_elevations
        self.elevations = ti.field(float, shape=self.n_elevations)
        self.elevation_inc = self.sky.elevational_aperture
        self.elevations.from_numpy(self.sky.elevation_centers)

        # Create a field which represents the buildings
        logger.info("Initializing buildings...")
        self.init_buildings()

        # Extract edges into Fields
        logger.info("Extracting edges...")
        self.extract_flat_edge_list()

    def ray_trace(self):
        # add edges to quadtree
        logger.info("Populating tree...")
        self.add_edges_to_tree()
        ti.sync()

        # Determine number of xy sensors needed
        logger.info("Determining XY Sensor count...")
        edge_count = self.edges.shape[0]
        sens_per_edge = self.edges.sensor_ct.to_numpy().astype(int)
        sensor_parent_ix = np.repeat(np.arange(edge_count), sens_per_edge)
        xy_sensor_count = sensor_parent_ix.shape[0]
        logger.info(f"XY sensor count: {xy_sensor_count}")

        # Construct Sensor data structures
        logger.info("Building dynamic hit tracking data structure...")
        self.sensor_root = ti.root.dense(ti.i, xy_sensor_count)
        self.ray_root = self.sensor_root.dense(ti.j, self.n_azimuths)
        self.hit_block = self.ray_root.dynamic(
            ti.k,
            2 ** (int(np.ceil(np.log2(self.n_ray_steps)))),
            chunk_size=64,
        )  # TODO: using dynamic lists causes big performance hit on gpu
        self.hits = Hit.field()
        self.hit_block.place(self.hits)
        self.xy_sensors = (
            XYSensor.field()
        )  # TODO: Why do I have to place hit_block first for compilation to work?
        self.sensor_root.place(self.xy_sensors)

        # Assign each sensor a parent id and then init data
        logger.info("Initializing xy-plane sensors...")
        self.xy_sensors.parent_edge_id.from_numpy(sensor_parent_ix)
        self.init_xy_sensors()
        ti.sync()

        # Determine how many xyz sensors are needed for data collection
        logger.info("Initializing xyz sensors...")
        xyz_cts = self.xy_sensors.xyz_sensor_ct.to_numpy()
        xyz_ends = np.cumsum(xyz_cts)  # use cumulative sums so that a sensor
        xyz_starts = np.roll(xyz_ends, shift=1)
        xyz_starts[0] = 0
        self.xy_sensors.xyz_sensor_start_ix.from_numpy(xyz_starts)
        xy_sensor_parent_ix = np.repeat(
            np.arange(xyz_cts.shape[0]), xyz_cts.astype(int)
        )

        xyz_sensor_count = xy_sensor_parent_ix.shape[0]
        logger.info(f"XYZ sensor count: {xyz_sensor_count}")
        self.xyz_sensors = XYZSensor.field()
        self.xyz_sensor_root = ti.root.dense(ti.i, xyz_sensor_count)
        self.xyz_sensor_root.place(self.xyz_sensors)
        self.xyz_view_root = self.xyz_sensor_root.bitmasked(
            ti.jk, (self.n_azimuths, self.n_elevations)
        )

        self.xyz_views = ti.field(dtype=uint1)
        self.ui1bitpacker = ti.BitpackedFields(max_num_bits=32)
        self.ui1bitpacker.place(self.xyz_views)
        self.xyz_view_root.place(self.ui1bitpacker)
        self.xyz_sensors.parent_sensor_id.from_numpy(xy_sensor_parent_ix)

        self.init_xyz_sensors()
        ti.sync()

        logger.info(f"XY rays: {xy_sensor_count * self.n_azimuths}")
        xyz_ray_ct = xyz_sensor_count * self.n_azimuths * self.n_elevations
        assert (
            xyz_ray_ct < 2**32
        ), f"This scene requires {xyz_ray_ct} rays which is greater than the currently supported max of 2^32 ~= 4e9."
        logger.info(f"XYZ rays: {xyz_ray_ct}")

        # OPTION 1: xy_trace then xyz_trace
        # # Ray trace in xy plane
        # logger.info("XY tracing...")
        # # self.xy_trace_divergent()
        # self.xy_trace()
        # ti.sync()
        # logger.info("XY tracing complete.")

        # # Ray trace using xyz data
        # logger.info("XYZ tracing...")
        # self.xyz_trace()
        # ti.sync()
        # logger.info("XYZ tracing complete.")

        # OPTION 2: xyz_trace all at once
        # Ray trace using xyz data
        logger.info("XYZ tracing...")
        self.xyz_trace_unified()
        ti.sync()
        logger.info("XYZ tracing complete.")

        # Timestep Accumulation
        # TODO: 8760 should be configurable resolution based on time-resolution
        # of sky's radiance results
        logger.info("Begin sky timestepping...")
        self.xyz_sensor_timesteps = ti.field(
            dtype=float, shape=(xyz_sensor_count, 8760)
        )
        self.timestep_sky()

        logger.info("Assemble results...")
        self.assemble_results_df()

        # Visualization
        self.sensor_3d_points = ti.Vector.field(3, dtype=float, shape=xyz_sensor_count)
        self.sensor_3d_colors = ti.Vector.field(3, dtype=float, shape=xyz_sensor_count)
        self.sensor_3d_rays = ti.Vector.field(
            3, dtype=float, shape=2 * (self.n_azimuths * self.n_elevations)
        )
        self.load_3d_points(timestep=0)
        self.load_3d_sensor_rays(0)

        ti.profiler.print_kernel_profiler_info()

    def init_gui(self):
        self.window = ti.ui.Window("UMI RayTrace", (1024, 1024), pos=(100, 100))
        self.gui = self.window.get_gui()
        self.canvas = self.window.get_canvas()
        self.scene = ti.ui.Scene()
        self.camera = ti.ui.Camera()
        self.camera.up(0, 1, 0)
        self.camera.position(0, 10, 0)
        self.camera.lookat(1, 10, 1)

    @ti.kernel
    def init_mesh_pts(self):
        for edge_ix in self.edges:
            edge = self.edges[edge_ix]
            height = edge.height
            start = edge.start
            end = edge.end
            mesh_pts_start_ix = 6 * edge_ix

            self.mesh_pts[mesh_pts_start_ix + 0] = ti.Vector([start.x, 0, start.y])
            self.mesh_pts[mesh_pts_start_ix + 1] = ti.Vector([end.x, 0, end.y])
            self.mesh_pts[mesh_pts_start_ix + 2] = ti.Vector([start.x, height, start.y])
            self.mesh_pts[mesh_pts_start_ix + 3] = ti.Vector([start.x, height, start.y])
            self.mesh_pts[mesh_pts_start_ix + 4] = ti.Vector([end.x, 0, end.y])
            self.mesh_pts[mesh_pts_start_ix + 5] = ti.Vector([end.x, height, end.y])

    def render_scene(self, sky=False):
        self.mesh_pts = ti.Vector.field(3, dtype=ti.f32, shape=self.edges.shape[0] * 6)
        self.init_mesh_pts()
        it = 0
        sensor_ix = 0
        use_auto_timestepper = True
        hr = 0

        sensor_ix_changed = True
        use_auto_timestepper_changed = True
        hr_changed = True
        while self.window.running:
            with self.gui.sub_window("Sensor selector", 0.1, 0.1, 0.8, 0.15):
                # Controls for sensor ix to display rays from
                old_ix = sensor_ix
                sensor_ix = self.gui.slider_int(
                    text="Sensor Index",
                    old_value=sensor_ix,
                    minimum=0,
                    maximum=self.xyz_sensors.shape[0],
                )

                # Control for disabling/enabling clock
                old_use_auto_timestepper = use_auto_timestepper
                use_auto_timestepper = self.gui.checkbox(
                    text="Use Automatic Timestepping",
                    old_value=use_auto_timestepper,
                )
                # Control for modifying current timestep
                old_hr = hr
                hr = self.gui.slider_int(
                    text="Current Timestep",
                    old_value=hr,
                    minimum=0,
                    maximum=8760 - 1,
                )

                if old_ix != sensor_ix:
                    sensor_ix_changed = True

                if sensor_ix_changed:
                    self.load_3d_sensor_rays(sensor_ix)
                    sensor_ix_changed = False

                if old_use_auto_timestepper != use_auto_timestepper:
                    use_auto_timestepper_changed = True
                if old_hr != hr:
                    hr_changed = True

                if use_auto_timestepper_changed or hr_changed:
                    self.load_3d_points(timestep=(hr % 8760))
                    self.sky.set_sky_colors(timestep=(hr % 8760))
                    use_auto_timestepper_changed = False
                    hr_changed = False

            self.camera.track_user_inputs(self.window, hold_key=ti.ui.RMB)
            self.scene.ambient_light((1, 1, 1))
            self.scene.particles(
                self.sensor_3d_points,
                radius=0.2,
                per_vertex_color=self.sensor_3d_colors,
            )
            self.scene.mesh(self.mesh_pts, color=(0.5, 0.5, 0.5), two_sided=True)
            if sky:
                if use_auto_timestepper:
                    if it % 18 == 0:
                        self.load_3d_points(timestep=(hr % 8760))
                        self.sky.set_sky_colors(timestep=(hr % 8760))
                        hr = hr + 1
                self.sky.add_dome_to_scene(self.scene)
            self.scene.lines(self.sensor_3d_rays, width=1, color=(1, 1, 1))
            self.scene.set_camera(self.camera)
            self.canvas.scene(self.scene)
            it = it + 1
            self.window.show()

    def assemble_results_df(self):
        results_df = pd.DataFrame(
            {
                "XYZSensor ID": np.arange(self.xyz_sensors.shape[0]),
                "Radiation": self.xyz_sensors.rad.to_numpy(),
                "XYSensor ID": self.xyz_sensors.parent_sensor_id.to_numpy(),
            }
        )
        xy_sensors_df = pd.DataFrame(
            {
                "XYSensor ID": np.arange(self.xy_sensors.shape[0]),
                "Edge ID": self.xy_sensors.parent_edge_id.to_numpy(),
            }
        )

        edges_df = pd.DataFrame(
            {
                "Edge ID": np.arange(self.edges.shape[0]),
                "Orientation": self.edges.orientation.to_numpy(),
                "Building ID": self.edges.building_id.to_numpy(),
            }
        )
        buildings_df = pd.DataFrame(
            {
                "Building ID": np.arange(self.buildings.shape[0]),
                "Archetype ID": self.buildings.archetype_ix.to_numpy(),
            }
        )
        self.results_df = (
            results_df.merge(
                xy_sensors_df,
                on="XYSensor ID",
            )
            .merge(
                edges_df,
                on="Edge ID",
            )
            .merge(buildings_df, on="Building ID")
        )

    def init_buildings(self):
        self.buildings = Building.field(shape=len(self.gdf))
        self.gdf["ARCHETYPE_ID"] = self.gdf[self.archetype_col].astype("category")
        archetype_ixs = self.gdf["ARCHETYPE_ID"].cat.codes.values
        heights = self.gdf[self.height_col].values
        self.buildings.archetype_ix.from_numpy(archetype_ixs)
        self.buildings.height.from_numpy(heights)

    def extract_flat_edge_list(self):
        """
        Extracts all edges to a flattened list
        # TODO: figure out if there is a vectorized manner instead of having to do it bldg by bldg
        """
        starts = []
        ends = []
        run_rises = []
        heights = []
        n_floors = []
        normals = []
        building_ids = []
        normal_fails = 0
        for i, _geom in enumerate(self.gdf.geometry):
            # Manual explode of geometry for multiploygon handling
            for geom in _geom.geoms if type(_geom) != Polygon else [_geom]:
                # Get the points from the boundary
                # shapely poly linestrings are closed, so we don't need the repeated point
                start_pts = np.array(geom.boundary.coords)[:-1]

                # TODO: everything after this point could be done in parallel.
                # Roll the points over
                end_pts = np.roll(start_pts, shift=-1, axis=0)

                # compute the slope components and unitize
                run_rise = end_pts - start_pts
                run_rise = run_rise / np.linalg.norm(run_rise, axis=1).reshape(-1, 1)

                # compute the normals for each edge
                # if we imagine the line segment as part of a plane which is perp
                # to the xy plane then we can take the cross product of the slope
                # and the k unit vector to get the perp vector which points to the outside.
                # note that this should never point inside because of the right-hand rule.
                cross_test = np.zeros(shape=(run_rise.shape[0], 3))
                cross_test[:, :2] = run_rise.copy()
                up = np.zeros_like(cross_test)
                up[:, 2] = 1
                normal = np.cross(
                    up, cross_test, axisa=1, axisb=1
                )  # this should point outward due to the winding of the polygons.
                normal = normal[:, :2]

                # some debug code for checking normal orientation
                # test_point = (points + next_points) / 2 + normal * 0.1 # move it 1 cm inside
                # for j in range(test_point.shape[0]):
                #     try:
                #         assert not geom.contains(Point(test_point[j,0], test_point[j,1]))
                #     except AssertionError:
                #         normal_fails = normal_fails + 1

                # append computed properties
                starts.append(start_pts)
                ends.append(end_pts)
                run_rises.append(run_rise)
                heights.append(
                    np.ones(start_pts.shape[0]) * self.gdf[self.height_col][i]
                )  # TODO: these could be found via a building parent ref
                n_floors.append(np.ones(start_pts.shape[0]) * self.gdf["N_FLOORS"][i])
                building_ids.append(np.ones(start_pts.shape[0]) * i)
                normals.append(normal)

        # Create flattened list of all edge data (i.e. flattened over buildings axis)
        starts = np.vstack(starts)
        ends = np.vstack(ends)
        run_rises = np.vstack(run_rises)
        normals = np.vstack(normals)
        heights = np.concatenate(heights)
        n_floors = np.concatenate(n_floors)
        building_ids = np.concatenate(building_ids)

        # Determine necessary sensor count per edge
        lengths = np.linalg.norm(starts - ends, axis=1)
        sensor_counts = (lengths - 2 * self.sensor_inset) / self.sensor_spacing
        sensor_counts = np.floor(np.where(sensor_counts >= 1, sensor_counts + 1, 0))
        sensor_ends = np.cumsum(sensor_counts)
        sensor_starts = np.roll(sensor_ends, shift=1)
        sensor_starts[0] = 0

        # Create the semantic edge objects in a dense flat struct field for better memory access
        self.edges = Edge.field(shape=heights.shape)
        # Load the scalar fields
        self.edges.building_id.from_numpy(building_ids)
        self.edges.height.from_numpy(heights)
        self.edges.n_floors.from_numpy(n_floors)
        self.edges.start.from_numpy(starts)
        self.edges.end.from_numpy(ends)
        self.edges.slopevec.from_numpy(run_rises)
        self.edges.normal.from_numpy(normals)
        self.edges.sensor_start_ix.from_numpy(sensor_starts)
        self.edges.sensor_end_ix.from_numpy(sensor_ends)
        self.edges.sensor_ct.from_numpy(sensor_counts)
        ti.sync()

        # Update some computed properties in parallel
        self.update_edge_properties()
        ti.sync()

        # Update the building flat list lookup indices
        edges_per_bldg = self.buildings.edge_ct.to_numpy()
        edge_ends = np.cumsum(edges_per_bldg)
        edge_starts = np.roll(edge_ends, shift=1)
        edge_starts[0] = 0
        self.buildings.edge_start_ix.from_numpy(edge_starts)
        self.buildings.edge_end_ix.from_numpy(edge_ends)

    @ti.kernel
    def update_edge_properties(self):
        """
        Update computed edge properties
        """
        for edge_ix in self.edges:
            # extract the endpoints/data

            # compute slope
            xm = self.edges[edge_ix].slopevec.x
            ym = self.edges[edge_ix].slopevec.y
            slope = ym / xm  # TODO: handle vert/hor lines

            # Compute the normal angle
            xn = self.edges[edge_ix].normal.x
            yn = self.edges[edge_ix].normal.y
            normal_theta = ti.atan2(yn, xn)

            # Determine orientation
            # in: 0deg, rotate 45: 45deg, divide by 90deg: 0.5, round down: 0 , out: 0 (east)
            # in: 45deg, rotate 45: 90deg, divide by 90deg: 1, round down: 1 , out: 1 (north)
            # in: 135deg, rotate 45: 180deg, divide by 90deg: 2, round down: 2 , out: 2 (west)
            # in: 225deg, rotate 45: 270deg, divide by 90deg: 3, round down: 3 , out: 3 (south)
            # in: 315deg, rotate 45: 360deg, divide by 90deg: 4, round down: 4 , out: 0 (south)
            orientation = ti.floor(
                ((((normal_theta + 2 * np.pi) % (2 * np.pi)) + np.pi / 4) / (np.pi / 2))
                % 4,
                dtype=ti.int8,
            )
            self.edges[edge_ix].orientation = orientation

            # Compute the azimuth start angle for any sensor placed on this edge
            # nb: this is offset by azimuth_inc/2 because the first ray should not be parallel
            # to the edge, i.e. this represents the azimuthal angle of incidence of the
            # first ray
            az_start_angle = normal_theta - np.pi * 0.5 + self.azimuth_inc / 2

            # Update the edge object
            self.edges[edge_ix].slope = slope
            self.edges[edge_ix].normal_theta = normal_theta
            self.edges[edge_ix].az_start_angle = az_start_angle

            # Add it to the parent buildings edge count
            self.buildings[self.edges[edge_ix].building_id].edge_ct += 1

    @ti.kernel
    def compute_edge_orientation_weights(self):
        for edge_ix in self.edges:
            edge = self.edges[edge_ix]
            building_id = edge.building_id
            building = self.buildings[building_id]
            normal_theta = (edge.normal_theta + 2 * np.pi) % (2 * np.pi)
            edge_start = edge.start
            edge_end = edge.end
            qualified_edge_length = ti.sqrt(
                (edge_start.x - edge_end.x) ** 2 + (edge_start.y - edge_end.y) ** 2
            )
            # edge must be > 2m to be considered
            if qualified_edge_length < 2:
                qualified_edge_length = 0.0
            self.edges[edge_ix].qualified_length = qualified_edge_length

            # compute the weight in each orientation, where normal_theta = 0 corresponds to east
            north_weight = 0.0
            east_weight = 0.0
            south_weight = 0.0
            west_weight = 0.0

            if normal_theta >= 0 and normal_theta <= np.pi / 2:
                # north and east
                north_weight = normal_theta / (np.pi / 2)
                east_weight = 1.0 - north_weight
            elif normal_theta > np.pi / 2 and normal_theta <= np.pi:
                # north and west
                north_weight = (np.pi - normal_theta) / (np.pi / 2)
                west_weight = 1.0 - north_weight
            elif normal_theta > np.pi and normal_theta <= 3 * np.pi / 2:
                # south and west
                south_weight = (normal_theta - np.pi) / (np.pi / 2)
                west_weight = 1.0 - south_weight
            elif normal_theta > 3 * np.pi / 2 and normal_theta <= 2 * np.pi:
                # south and east
                south_weight = (2 * np.pi - normal_theta) / (np.pi / 2)
                east_weight = 1.0 - south_weight
            else:
                # this should never happen
                assert False

            # store the weights
            self.buildings[building_id].north_weight += (
                north_weight * qualified_edge_length
            )
            self.buildings[building_id].east_weight += (
                east_weight * qualified_edge_length
            )
            self.buildings[building_id].south_weight += (
                south_weight * qualified_edge_length
            )
            self.buildings[building_id].west_weight += (
                west_weight * qualified_edge_length
            )
            self.buildings[building_id].qualified_perim_length += qualified_edge_length

        ti.sync()
        for edge_ix in self.edges:
            edge = self.edges[edge_ix]
            building = self.buildings[edge.building_id]
            building_perim_length = building.qualified_perim_length
            weight = edge.qualified_length / building_perim_length
            # if an edge would be lss than 1.5% of the building perimeter, it is not considered
            if weight < 0.015:
                weight = 0.0
            self.edges[edge_ix].weight = weight
            self.buildings[edge.building_id].qualified_edge_weight_sum += weight

        ti.sync()
        # Renormalize edge weights after eliminating small ones
        for edge_ix in self.edges:
            edge = self.edges[edge_ix]
            building = self.buildings[edge.building_id]
            weight = edge.weight / building.qualified_edge_weight_sum
            self.edges[edge_ix].weight = weight

        ti.sync()
        # normalize the cardinal weights
        for building_ix in self.buildings:
            building = self.buildings[building_ix]
            weight_sum = (
                building.north_weight
                + building.east_weight
                + building.south_weight
                + building.west_weight
            )
            self.buildings[building_ix].north_weight /= weight_sum
            self.buildings[building_ix].east_weight /= weight_sum
            self.buildings[building_ix].south_weight /= weight_sum
            self.buildings[building_ix].west_weight /= weight_sum

        ti.sync()

    @ti.kernel
    def add_edges_to_tree(self):
        """
        This function determines where each line crosses a node threshold and
        updates that node's height accordingly.
        """
        for edge_ix in self.edges:
            # extract the endpoints/data
            edge = self.edges[edge_ix]
            x0 = edge.start.x
            y0 = edge.start.y
            x1 = edge.end.x
            y1 = edge.end.y
            h = edge.height
            slope = edge.slope
            normal = edge.normal

            # Sort the end points
            x_min = ti.min(x0, x1)
            x_max = ti.max(x0, x1)
            y_min = ti.min(y0, y1)
            y_max = ti.max(y0, y1)

            # Find the nearest node boundary line
            # TODO: currently only supports node width of 1!
            x_start = ti.ceil(x_min, dtype=int)
            x_end = ti.floor(x_max, dtype=int)
            y_start = ti.ceil(y_min, dtype=int)
            y_end = ti.floor(y_max, dtype=int)

            # Count the number of grid lines to check
            n_x_thresholds = 1 + x_end - x_start
            n_y_thresholds = 1 + y_end - y_start

            # Compute the grid line crossings and update node heights
            # TODO: DIVERGENCE WARNING
            # TODO: DIVERGENCE WARNING
            # TODO: DIVERGENCE WARNING
            # TODO: DIVERGENCE WARNING
            # but it's not so bad :)
            for x_int_ix in range(n_x_thresholds):
                x = x_start + x_int_ix  # TODO: currently only supports node width of 1
                y = slope * (x - x0) + y0
                y_ix = ti.floor(y, int)  # TODO: currently only supports node width of 1

                # Add height to quadtree if the edge is taller than the existing edge
                ti.atomic_max(self.nodes[x - 1, y_ix].height, h)  # update left node
                ti.atomic_max(self.nodes[x, y_ix].height, h)  # update right node

                # Thicken the edges
                if edge.normal_theta >= 0.0 and edge.normal_theta < np.pi / 4:
                    ti.atomic_max(self.nodes[x - 2, y_ix].height, h)  # update left node
                    ti.atomic_max(
                        self.nodes[x - 1, y_ix].height, h
                    )  # update right node
                elif (
                    edge.normal_theta >= 3 * np.pi / 4
                    and edge.normal_theta < 5 * np.pi / 4
                ):
                    ti.atomic_max(self.nodes[x, y_ix].height, h)  # update left node
                    ti.atomic_max(
                        self.nodes[x + 1, y_ix].height, h
                    )  # update right node
                elif (
                    edge.normal_theta >= 7 * np.pi / 4 and edge.normal_theta < 2 * np.pi
                ):
                    ti.atomic_max(self.nodes[x - 2, y_ix].height, h)  # update left node
                    ti.atomic_max(
                        self.nodes[x - 1, y_ix].height, h
                    )  # update right node

            for y_int_ix in range(n_y_thresholds):
                y = y_start + y_int_ix  # TODO: currently only supports node width of 1
                x = (1 / slope) * (y - y0) + x0
                x_ix = ti.floor(x, int)  # TODO: currently only supports node width of 1

                # Add height to quadtree if the edge is taller than the existing edge
                ti.atomic_max(self.nodes[x_ix, y - 1].height, h)  # update lower node
                ti.atomic_max(self.nodes[x_ix, y].height, h)  # update upper node

                # Thicken the edges
                if edge.normal_theta >= np.pi / 4 and edge.normal_theta < 3 * np.pi / 4:
                    ti.atomic_max(
                        self.nodes[x_ix, y - 2].height, h
                    )  # update lower node
                    ti.atomic_max(
                        self.nodes[x_ix, y - 1].height, h
                    )  # update upper node
                elif (
                    edge.normal_theta >= 5 * np.pi / 4
                    and edge.normal_theta < 7 * np.pi / 4
                ):
                    ti.atomic_max(self.nodes[x_ix, y].height, h)  # update lower node
                    ti.atomic_max(
                        self.nodes[x_ix, y + 1].height, h
                    )  # update upper node

    @ti.kernel
    def init_xy_sensors(self):
        """
        Configures the location/parent information for every sensor by copying data over
        Relies on the repeat edge field edge_sensor_parent_ix
        """
        for sensor_ix in self.xy_sensors:
            # Locate the sensor in the original field and copy the parent id over
            parent_id = self.xy_sensors[sensor_ix].parent_edge_id
            edge = self.edges[parent_id]

            # get the parent slope over
            slope = edge.slopevec

            # Determine the inset edge gap for the sensor
            start_gap = (
                slope * self.sensor_inset
            )  # TODO: this could be stored with parent # TODO: this could be centered

            # Determine which sensor this is along a the parent edge
            gap_ct = sensor_ix - edge.sensor_start_ix

            # compute the distance from the edge start vertex
            distance = start_gap + gap_ct * slope * self.sensor_spacing

            # Copy the parent edge start vertex over
            start_loc = edge.start

            # Copy the parent edge normal over
            normal = edge.normal

            # Set the new location by moving along edge the appropriate amount
            # and then an offset distance away from the wall following the normal
            self.xy_sensors[sensor_ix].loc = (
                start_loc + distance + normal * self.sensor_normal_offset
            )

            # Store the parent id
            self.xy_sensors[sensor_ix].xyz_sensor_ct = edge.n_floors

            # Store the parent id
            self.xy_sensors[sensor_ix].parent_edge_id = parent_id

    @ti.kernel
    def init_xyz_sensors(self):
        for sensor_ix in self.xyz_sensors:
            parent_id = self.xyz_sensors[sensor_ix].parent_sensor_id
            xy_sensor = self.xy_sensors[parent_id]
            floor_ix = sensor_ix - xy_sensor.xyz_sensor_start_ix
            # Sensors should be in floor midpoint, so use 1.5xf2f
            height = floor_ix * 1.5 * self.f2f_height
            self.xyz_sensors[sensor_ix].height = height

    @ti.kernel
    def xy_trace(self):
        ray_step_size = self.ray_step_size  # TODO: will cause duplicate collisions
        steps_per_loop = self.steps_per_unroll_loop

        # Break ray stepping up into portions of ray
        # In order to prevent ti.ndrange overflow (there may be several billion checks to make even in 2d)
        # Use loop unnrolling via ti.static to keep inner loop parallelized
        for loop_ix in ti.static(range(N_LOOPS_TO_UNROLL)):
            step_offset = loop_ix * steps_per_loop
            for sensor_ix, az_ix, ray_step_ix in ti.ndrange(
                self.xy_sensors.shape[0], self.n_azimuths, steps_per_loop
            ):
                # Compute the rays's azimuth angle
                sensor = self.xy_sensors[sensor_ix]

                az_angle = (
                    self.azimuths[az_ix]
                    + self.edges[sensor.parent_edge_id].az_start_angle
                )

                # Compute the ray's xy-plane slope
                # TODO: precompute as a lookup in init based off of n_azimuths?
                dx = ti.cos(az_angle)
                dy = ti.sin(az_angle)
                slope = ti.Vector([dx, dy])

                # Get the ray's starting point
                start = sensor.loc

                # Length of ray to check
                distance = ray_step_size * (ray_step_ix + step_offset)

                # Initializing the next location to check
                next_loc = start + distance * slope

                # Tester for ray termination
                in_domain = (
                    (next_loc.x > 0)
                    and (next_loc.y > 0)
                    and (next_loc.x < self.width)
                    and (next_loc.y < self.length)
                )
                if in_domain:
                    # Get ray terminus node index
                    x_loc_ix = ti.floor(
                        next_loc.x, int
                    )  # TODO: assumes grid spacing = 1
                    y_loc_ix = ti.floor(next_loc.y, int)

                    # Check if node is active
                    if ti.is_active(self.tree_leaves, [x_loc_ix, y_loc_ix]) == 1:
                        # Get the node height and register a hit
                        node_height = self.nodes[x_loc_ix, y_loc_ix].height
                        # TODO: this is causing a large performance hit on gpu backend
                        self.hits[sensor_ix, az_ix].append(
                            Hit(
                                loc_x_ix=x_loc_ix,
                                loc_y_ix=y_loc_ix,
                                height=node_height,
                                distance=distance,  # TODO: should this use the node centroid distance instead?
                            )  # TODO: assumes a  grid spacing = 1
                        )
                        self.xy_sensors[sensor_ix].hit_count += 1

    @ti.kernel
    def xy_trace_divergent(self):
        max_ray_length = self.max_ray_length
        ray_step_size = self.ray_step_size  # TODO: will cause duplicate collisions

        for sensor_ix, az_ix in ti.ndrange(self.xy_sensors.shape[0], self.n_azimuths):
            # Compute the rays's azimuth angle
            sensor = self.xy_sensors[sensor_ix]

            az_angle = (
                self.azimuths[az_ix] + self.edges[sensor.parent_edge_id].az_start_angle
            )

            # Compute the ray's xy-plane slope
            # TODO: precompute as a lookup in init based off of n_azimuths?
            dx = ti.cos(az_angle)
            dy = ti.sin(az_angle)
            slope = ti.Vector([dx, dy])

            # Get the ray's starting point
            start = sensor.loc

            # Tracker for ray extension
            ray_step_ix = 0.0

            # Initializing the next location to check
            distance = ray_step_ix * ray_step_size
            next_loc = start + distance * slope

            # Tester for ray termination
            in_domain = (
                (next_loc.x > 0)
                and (next_loc.y > 0)
                and (next_loc.x < self.width)
                and (next_loc.y < self.length)
                and distance < max_ray_length
            )
            while in_domain:
                # Get ray terminus node index
                x_loc_ix = ti.floor(next_loc.x, int)  # TODO: assumes grid spacing = 1
                y_loc_ix = ti.floor(next_loc.y, int)

                # Check if node is active
                if ti.is_active(self.tree_leaves, [x_loc_ix, y_loc_ix]) == 1:
                    # Get the node height and register a hit
                    node_height = self.nodes[x_loc_ix, y_loc_ix].height
                    # TODO: this is causing a large performance hit on gpu backend
                    self.hits[sensor_ix, az_ix].append(
                        Hit(
                            loc_x_ix=x_loc_ix,
                            loc_y_ix=y_loc_ix,
                            height=node_height,
                            distance=distance,  # TODO: should this use the node centroid distance instead?
                        )  # TODO: assumes a  grid spacing = 1
                    )
                    self.xy_sensors[sensor_ix].hit_count += 1

                # Advance the ray stepper
                ray_step_ix = ray_step_ix + 1.0

                # Compute the new length
                distance = ray_step_ix * ray_step_size

                # Compute the new location
                next_loc = start + distance * slope

                # Tester for ray termination
                in_domain = (
                    (next_loc.x > 0)
                    and (next_loc.y > 0)
                    and (next_loc.x < self.width)
                    and (next_loc.y < self.length)
                    and distance < max_ray_length
                )

    @ti.kernel
    def xyz_trace(self):
        for sensor_ix, az_ix, el_ix in ti.ndrange(
            self.xyz_sensors.shape[0], self.n_azimuths, self.n_elevations
        ):
            # get the xyz sensors corresponding xy sensor
            parent_sensor_id = self.xyz_sensors[sensor_ix].parent_sensor_id

            # get the xyz sensor's height
            xyz_sensor_height = self.xyz_sensors[sensor_ix].height

            # determine how many hits need to be checked based off of xy sensors hit table
            # n_hits_to_check = self.hits[parent_sensor_id, az_ix].length()
            n_hits_to_check = self.xy_sensors[parent_sensor_id].hit_count
            el_angle = self.elevations[el_ix]  # TODO:  or store and use slopes?

            # Initiate an iterator so we can bail out early
            # via a while loop, rather than using automatic iteration
            hit_ix = 0
            # create a flag for when a hit has been found
            hit_found = 0
            while hit_ix < n_hits_to_check and hit_found != 1:
                # Extract an xy hit and its properties
                hit = self.hits[parent_sensor_id, az_ix, hit_ix]
                hit_height = hit.height
                hit_distance = hit.distance
                # compute the height diff for the current xyz sensor
                height_diff = hit_height - xyz_sensor_height

                # compute the angle
                theta = ti.atan2(
                    height_diff, hit_distance
                )  # TODO: would using a slope divison be more performant?

                # Check if the sensor-to-other-building angle is greater than the sensor-to-sky-patch angle
                if theta > el_angle:
                    # Indicate a bail out if the building is obstructing
                    hit_found = 1

                # increment the hit ix iterator
                hit_ix = hit_ix + 1

            # If no obstructions found, then add the result in
            if hit_found != 1:
                self.xyz_sensors[sensor_ix].rad += 1  # TODO: look up sky matrix
                # Store a hit mask
                self.xyz_views[sensor_ix, az_ix, el_ix] = 1

    @ti.kernel
    def xyz_trace_unified(self):
        for sensor_ix, az_ix, el_ix in ti.ndrange(
            self.xyz_sensors.shape[0], self.n_azimuths, self.n_elevations
        ):
            # get the xyz sensors corresponding xy sensor
            parent_sensor_id = self.xyz_sensors[sensor_ix].parent_sensor_id
            parent_sensor = self.xy_sensors[parent_sensor_id]

            # get the xyz sensor's height
            xyz_sensor_height = self.xyz_sensors[sensor_ix].height

            # Get Az/El Angles
            # TODO: precompute these? or store slopes?
            el_angle = self.elevations[el_ix]

            az_angle = (
                self.azimuths[az_ix]
                + self.edges[parent_sensor.parent_edge_id].az_start_angle
            )

            # Compute the ray's xy-plane slope
            # TODO: precompute as a lookup in init based off of n_azimuths?
            dx = ti.cos(az_angle)
            dy = ti.sin(az_angle)
            slope = ti.Vector([dx, dy])

            # Get the ray's starting point
            start = parent_sensor.loc

            distance = self.trace_xyz_ray(start, slope, el_angle, xyz_sensor_height)

            # If no obstructions found, then add the result in
            if distance < 0:
                self.xyz_sensors[
                    sensor_ix
                ].rad += (
                    1  # TODO: look up sky matrix # Decide if we even want to keep this.
                )
                # Store a hit mask
                self.xyz_views[sensor_ix, az_ix, el_ix] = 1
                # TODO: track hit location

    @ti.func
    def trace_xyz_ray(
        self,
        start: ti.math.vec2,
        slope: ti.math.vec2,
        el_angle: float,
        xyz_sensor_height: float,
    ) -> float:
        # Tracker for ray extension
        ray_step_ix = 0.0

        # Initializing the next location to check
        distance = ray_step_ix * self.ray_step_size
        next_loc = start + distance * slope

        # Tester for ray termination
        in_domain = (
            (next_loc.x > 0)
            and (next_loc.y > 0)
            and (next_loc.x < self.width)
            and (next_loc.y < self.length)
            and distance < self.max_ray_length
        )

        hit_found = 0
        while in_domain and hit_found != 1:
            # Get ray terminus node index
            x_loc_ix = ti.floor(next_loc.x, int)  # TODO: assumes grid spacing = 1
            y_loc_ix = ti.floor(next_loc.y, int)

            # Check if node is active
            if ti.is_active(self.tree_leaves, [x_loc_ix, y_loc_ix]) == 1:
                # Get the height of the node in the xy plane
                node_height = self.nodes[x_loc_ix, y_loc_ix].height

                # Compute the height difference to the edge crossed
                height_diff = node_height - xyz_sensor_height

                # compute the angle
                theta = ti.atan2(
                    height_diff, distance
                )  # TODO: would using a slope divison be more performant?

                # Check if the sensor-to-other-building angle is greater than the sensor-to-sky-patch angle
                if theta > el_angle:
                    # Indicate a bail out if the building is obstructing
                    hit_found = 1

            # Advance the ray stepper
            ray_step_ix = ray_step_ix + 1.0

            # Compute the new length
            distance = ray_step_ix * self.ray_step_size

            # Compute the new location
            next_loc = start + distance * slope

            # Tester for ray termination
            in_domain = (
                (next_loc.x > 0)
                and (next_loc.y > 0)
                and (next_loc.x < self.width)
                and (next_loc.y < self.length)
                and distance < self.max_ray_length
            )

        if hit_found == 0:
            distance = -1
        # TODO: this is bad!
        return distance

    @ti.kernel
    def timestep_sky(self):
        """
        Responsible timestepping each ray hit and accumulating the results into the sensors
        # TODO: this is pretty slow, 11s much slower than tracing,
        # but I think it should be faster.
        """
        for sensor_ix, az_ix, el_ix in self.xyz_views:
            xyz_sensor = self.xyz_sensors[sensor_ix]
            parent_xy_sen = self.xy_sensors[xyz_sensor.parent_sensor_id]
            parent_edge = self.edges[parent_xy_sen.parent_edge_id]
            az_angle = (
                self.azimuths[az_ix] + parent_edge.az_start_angle
            )  # the angle the ray was emitted at

            # convert elevation angle to quantized index
            # TODO: this could be replaced with the edge storing an offset which just gets added to az_ix to save avoid division op
            sky_patch_az_ix = (
                ti.cast(ti.floor(az_angle / self.azimuth_inc), dtype=int)
                % self.sky.n_azimuths
            )

            # Compute incidence factor
            incidence_factor = ti.cos(
                ti.abs(az_angle - parent_edge.normal_theta)
            ) * ti.cos(self.elevations[el_ix])
            for timestep in range(8760):
                # Get the irradiance of a normal surface for the given sky patch
                E = self.sky.normal_irradiance_field[el_ix, sky_patch_az_ix, timestep]
                # Add the irradiance in for that timestep after adjusting for the angle of incidence.
                self.xyz_sensor_timesteps[sensor_ix, timestep] += E * incidence_factor

    @ti.kernel
    def load_3d_points(self, timestep: int):
        for sensor_ix in self.xyz_sensors:
            xyz_sensor = self.xyz_sensors[sensor_ix]
            parent_xy_sen = self.xy_sensors[
                self.xyz_sensors[sensor_ix].parent_sensor_id
            ]
            timestep_result = self.xyz_sensor_timesteps[sensor_ix, timestep]
            self.sensor_3d_points[sensor_ix].x = parent_xy_sen.loc.x
            self.sensor_3d_points[sensor_ix].y = xyz_sensor.height
            self.sensor_3d_points[sensor_ix].z = parent_xy_sen.loc.y

            c_norm = ti.min(
                timestep_result / 1000.0,
                # ti.max(
                #     (xyz_sensor.rad - 650.0)
                #     / (self.n_azimuths * self.n_elevations - 650),
                #     0.0,
                # ),
                1.0,
            )
            self.sensor_3d_colors[sensor_ix].x = 1
            self.sensor_3d_colors[sensor_ix].y = 1 - c_norm
            self.sensor_3d_colors[sensor_ix].z = 1 - c_norm

    @ti.kernel
    def load_3d_sensor_rays(self, sensor_ix: int):
        for az_ix, el_ix in ti.ndrange(self.n_azimuths, self.n_elevations):
            ray_ix = az_ix * self.n_elevations + el_ix
            # get the xyz sensors corresponding xy sensor
            parent_sensor_id = self.xyz_sensors[sensor_ix].parent_sensor_id
            parent_sensor = self.xy_sensors[parent_sensor_id]

            # get the xyz sensor's height
            xyz_sensor_height = self.xyz_sensors[sensor_ix].height

            el_angle = self.elevations[el_ix]  # TODO: or store and use slopes?

            az_angle = (
                self.azimuths[az_ix]
                + self.edges[parent_sensor.parent_edge_id].az_start_angle
            )

            # Compute the ray's xy-plane slope
            dx = ti.cos(
                az_angle
            )  # TODO: precompute as a lookup in init based off of n_azimuths?
            dy = ti.sin(az_angle)
            slope = ti.Vector([dx, dy])

            # Get the ray's starting point
            start = parent_sensor.loc

            distance = self.trace_xyz_ray(start, slope, el_angle, xyz_sensor_height)

            if distance < 0:
                # hide the ray by setting the target to the source
                self.sensor_3d_rays[2 * ray_ix].x = parent_sensor.loc.x
                self.sensor_3d_rays[2 * ray_ix].y = xyz_sensor_height
                self.sensor_3d_rays[2 * ray_ix].z = parent_sensor.loc.y
            else:
                # the ray hit something
                self.sensor_3d_rays[2 * ray_ix].x = (
                    distance * slope.x + parent_sensor.loc.x
                )
                self.sensor_3d_rays[2 * ray_ix].y = (
                    xyz_sensor_height
                    + ti.tan(el_angle)
                    * ti.sqrt(slope.x * slope.x + slope.y * slope.y)
                    * distance
                )
                self.sensor_3d_rays[2 * ray_ix].z = (
                    distance * slope.y + parent_sensor.loc.y
                )

            self.sensor_3d_rays[2 * ray_ix + 1].x = parent_sensor.loc.x
            self.sensor_3d_rays[2 * ray_ix + 1].y = xyz_sensor_height
            self.sensor_3d_rays[2 * ray_ix + 1].z = parent_sensor.loc.y

    @ti.kernel
    def print_column_stats(self, xy_sensor: int):
        """
        Given an xy sensor, this function will print out the
        stats for each of the xyz sensors in the column above it.
        Useful for debugging.
        """
        print(f"\ncolumn for xy sensor {xy_sensor}")

        # Get the XY Sensor
        sensor = self.xy_sensors[xy_sensor]

        # Find where the stack of xyz sensors starts
        xyz_s = sensor.xyz_sensor_start_ix

        # Get the number of sensors above (from n floors)
        xyz_e = xyz_s + sensor.xyz_sensor_ct

        for xyz_sensor_ix in range(xyz_s, xyz_e):
            # Get the corresponding sensor
            sen = self.xyz_sensors[xyz_sensor_ix]
            # Get the radiation
            rad = sen.rad
            print(f"Floor {xyz_sensor_ix}: {rad} rad")
        ti.sync()

        # Check for hits for each individual ray
        for xyz_sensor_ix in range(xyz_s, xyz_e):
            sum = 0.0
            for az_ix, el_ix in ti.ndrange(self.n_azimuths, self.n_elevations):
                # For each ray, check if it's a hit
                if ti.is_active(self.xyz_view_root, [xyz_sensor_ix, az_ix, el_ix]) == 1:
                    sum = sum + 1
            print(f"Floor {xyz_sensor_ix}: {sum} hits")

    def get_sensor_hits_as_im(self, sensor_ix: int) -> ti.ScalarField:
        im = ti.field(float, shape=(2**self.depth, 2**self.depth))
        self.set_sensor_hits_im_kernel(im, sensor_ix=sensor_ix)

        return im

    def get_sensor_hits_as_pts(self, sensor_ix: int) -> ti.ScalarField:
        cur = ti.field(int, shape=())
        circs = ti.Vector.field(
            2, dtype=float, shape=self.xy_sensors[sensor_ix].hit_count
        )
        self.set_sensor_hits_pts_kernel(sensor_ix, pts=circs, cur=cur)
        return circs

    def get_colored_hit_map(self, sensor_ix: int):
        sensor_im = self.get_sensor_hits_as_im(sensor_ix)
        color_im = ti.Vector.field(
            3, dtype=float, shape=(2**self.depth, 2**self.depth)
        )
        self.combine_edge_and_sensor_hit_maps_kernel(
            sensor_im=sensor_im, color_im=color_im
        )

        return color_im

    def get_sensor_to_first_hit_rays(self, sensor_ix: int) -> ti.math.vec2:
        first_hit_points = ti.Vector.field(2, dtype=float, shape=self.n_azimuths + 1)
        indices = ti.field(int, shape=2 * self.n_azimuths)
        self.set_first_hit_points_kernel(
            sensor_ix=sensor_ix, pts=first_hit_points, indices=indices
        )
        return first_hit_points, indices

    @ti.kernel
    def set_first_hit_points_kernel(
        self, sensor_ix: int, pts: ti.template(), indices: ti.template()
    ):
        # TODO: ASSUMES POINTS ARE SORTED
        pts[self.n_azimuths] = self.xy_sensors[sensor_ix].loc
        for az_ix in range(self.n_azimuths):
            if self.hits[sensor_ix, az_ix].length() > 0:
                loc = self.hits[
                    sensor_ix, az_ix, 0
                ].centroid()  # TODO: Assumes a 1m grid spacing
                pts[az_ix] = loc
            else:
                az_angle = (
                    self.azimuths[az_ix]
                    + self.edges[
                        self.xy_sensors[sensor_ix].parent_edge_id
                    ].az_start_angle
                )
                dx = ti.cos(az_angle)  # TODO: precompute as a lookup
                dy = ti.sin(az_angle)  # TODO: precompute as a lookup
                slope = ti.Vector([dx, dy])
                pts[az_ix] = pts[self.n_azimuths] + slope * 500
            indices[az_ix * 2] = self.n_azimuths
            indices[az_ix * 2 + 1] = az_ix

    @ti.kernel
    def set_sensor_hits_pts_kernel(
        self, sensor_ix: int, pts: ti.template(), cur: ti.template()
    ):
        for az_ix in range(self.n_azimuths):
            for hit_ix in range(self.hits[sensor_ix, az_ix].length()):
                hit = self.hits[sensor_ix, az_ix, hit_ix]
                pts[
                    ti.atomic_add(cur[None], 1)
                ] = hit.centroid()  # TODO: Assumes a 1m grid spacing

    @ti.kernel
    def set_sensor_hits_im_kernel(self, im: ti.template(), sensor_ix: int):
        for az_ix in range(self.n_azimuths):
            for hit_ix in range(self.hits[sensor_ix, az_ix].length()):
                hit = self.hits[sensor_ix, az_ix, hit_ix]
                im[hit.loc_x_ix, hit.loc_y_ix] = 1

    @ti.kernel
    def combine_edge_and_sensor_hit_maps_kernel(
        self,
        sensor_im: ti.template(),
        color_im: ti.template(),
    ):
        for i, j in color_im:
            if ti.is_active(self.tree_leaves, [i, j]):
                color_im[i, j] = ti.Vector([1, 1, 1])
            if sensor_im[i, j] > 0:
                color_im[i, j] = ti.Vector([1, 0, 0])

    @property
    def tree_leaves(self):
        return self.levels[-1]

    @property
    def tree_root(self):
        return self.levels[0]


"""
Some visualization helper functions/kernels
"""


@ti.kernel
def set_edge_verts_kernel(
    edge_starts: ti.template(), edge_ends: ti.template(), edge_verts: ti.template()
):
    for i in range(edge_starts.shape[0]):
        edge_verts[2 * i] = ti.Vector([edge_starts[i, 0], edge_starts[i, 1]])
        edge_verts[2 * i + 1] = ti.Vector([edge_ends[i, 0], edge_ends[i, 1]])


@ti.kernel
def set_edge_colors_kernel(edge_starts: ti.template(), edge_colors: ti.template()):
    for i in range(edge_starts.shape[0]):
        edge_colors[2 * i] = ti.Vector([ti.random(), ti.random(), ti.random()]) * 0.5
        edge_colors[2 * i + 1] = edge_colors[2 * i]


@ti.kernel
def zoom_pan_im_kernel(
    source_im: ti.template(), target_im: ti.template(), x_offset: int, y_offset: int
):
    for i, j in target_im:
        target_im[i, j] = source_im[i + x_offset, j + y_offset]


# TODO: make a parent function which copies and zooms at the same time so the underlying points don't need to be reassembled.
@ti.kernel
def zoom_pan_pts_kernel(
    source_pts: ti.template(),
    zoom: float,
    x_offset: int,
    y_offset: int,
    zoom_base: float,
):
    for i in source_pts:
        source_pts[i] = (source_pts[i] - ti.Vector([x_offset, y_offset])) / (
            zoom * zoom_base
        )


def zoom_pan_im(source_im, zoom: float, x_offset: int, y_offset: int):
    target_im = ti.Vector.field(
        3,
        float,
        shape=(int(source_im.shape[0] * zoom), int(source_im.shape[0] * zoom)),
    )
    zoom_pan_im_kernel(
        source_im=source_im,
        target_im=target_im,
        x_offset=x_offset,
        y_offset=y_offset,
    )
    return target_im


def old_debug_viz_loop(tracer):
    ############
    # debug viz
    window = ti.ui.Window("GIS", (1024, 1024), pos=(50, 50))
    canv = window.get_canvas()
    ui = window.get_gui()

    # TODO: move edge points into class
    edge_ct = tracer.edge_starts.shape[0]
    borderline_verts = ti.Vector.field(2, dtype=float, shape=(2 * edge_ct))
    # borderline_colors = ti.Vector.field(3, dtype=float, shape=(2 * edge_ct))

    # set_edge_colors_kernel(tracer.edge_starts, borderline_colors)
    set_edge_verts_kernel(tracer.edge_starts, tracer.edge_ends, borderline_verts)

    sensor_ix = 0
    circs = tracer.get_sensor_hits_as_pts(sensor_ix)
    hit_lines, indices = tracer.get_sensor_to_first_hit_rays(sensor_ix)

    zoom = 1
    x_offset = 0
    y_offset = 0

    controls_changed = True
    while window.running:
        with ui.sub_window("Sensor selector", 0.1, 0.1, 0.8, 0.15):
            old_ix = sensor_ix
            sensor_ix = ui.slider_int(
                text="Sensor Index",
                old_value=sensor_ix,
                minimum=0,
                maximum=tracer.xy_sensors.shape[0],
            )
            if old_ix != sensor_ix:
                controls_changed = True

            old_zoom = zoom
            zoom = ui.slider_float(
                text="Zoom Level",
                old_value=zoom,
                minimum=0.0001,
                maximum=1,
            )
            if old_zoom != zoom:
                controls_changed = True

            old_x_offset = x_offset
            x_offset = ui.slider_int(
                text="X Offset",
                old_value=x_offset,
                minimum=0,
                maximum=int(tracer.nodes.shape[0] - zoom * tracer.nodes.shape[0]),
            )
            if old_x_offset != x_offset:
                controls_changed = True

            old_y_offset = y_offset
            y_offset = ui.slider_int(
                text="Y Offset",
                old_value=y_offset,
                minimum=0,
                maximum=int(tracer.nodes.shape[0] - zoom * tracer.nodes.shape[0]),
            )
            if old_y_offset != y_offset:
                controls_changed = True

            if controls_changed:
                circs = tracer.get_sensor_hits_as_pts(sensor_ix)
                hit_verts, _ = tracer.get_sensor_to_first_hit_rays(sensor_ix)
                set_edge_verts_kernel(
                    tracer.edge_starts,
                    tracer.edge_ends,
                    borderline_verts,
                )

                zoom_pan_pts_kernel(
                    source_pts=circs,
                    zoom=zoom,
                    zoom_base=tracer.nodes.shape[0],
                    x_offset=x_offset,
                    y_offset=y_offset,
                )
                zoom_pan_pts_kernel(
                    source_pts=hit_verts,
                    zoom=zoom,
                    zoom_base=tracer.nodes.shape[0],
                    x_offset=x_offset,
                    y_offset=y_offset,
                )
                zoom_pan_pts_kernel(
                    source_pts=borderline_verts,
                    zoom=zoom,
                    zoom_base=tracer.nodes.shape[0],
                    x_offset=x_offset,
                    y_offset=y_offset,
                )
            controls_changed = False

        canv.lines(
            borderline_verts, 0.001, color=(1, 1, 1)
        )  # per_vertex_color=borderline_colors)
        canv.lines(hit_verts, 0.002, color=(1, 0, 0), indices=indices)
        canv.circles(circs, radius=0.002, color=(1, 0, 0))

        window.show()


def epw_to_wea(epw_inpath, wea_outpath):
    wea = Wea.from_epw_file(epw_inpath)
    wea.write(str(wea_outpath))


@ti.data_oriented
class Sky:
    """
    The Sky class is used for managing the radiance and normal irradiance for a sky derived from
    a provided weather file.

    The class handles converting a Tregenza/Reinhart sky to a parallel/meridian (i.e. lat/lon lines)
    subdivision.

    A desired number of azimuths are specified; the the number of elevations will be derived from the
    sky subdivision factor.

    The dome radius factor is used exclusively for rendering in a 3D scene.
    """

    def __init__(
        self,
        epw: EPW,
        mfactor: int,
        n_azimuths: int,
        dome_radius: float,
        run_conversion=True,
    ):
        logger.info("Beginning sky matrix extraction...")
        self.epw = epw
        self.mfactor = mfactor
        self.n_azimuths = n_azimuths

        # TODO: these properties are duplicate computed in the conversion method
        # azimuthal aperture is trivial
        self.azimuthal_aperture = np.radians(360) / self.n_azimuths
        base_patches_per_elevation = np.array(
            [30, 30, 24, 24, 18, 12, 6], dtype=np.int64
        )
        patches_per_elevation = (
            base_patches_per_elevation.repeat(self.mfactor) * self.mfactor
        )
        self.n_elevations = patches_per_elevation.shape[0]
        self.elevational_aperture = np.radians(90 - 6) / (self.n_elevations)
        self.elevation_centers = (
            self.elevational_aperture * np.arange(self.n_elevations)
            + self.elevational_aperture / 2
        )
        if run_conversion:
            wea_fn = Path(self.epw.file_path).stem
            self.wea_fp = Path("data") / "mtxs" / f"{wea_fn}.wea"
            logger.info("Converting EPW to WEA...")
            # TODO: make sure matrices are identical
            # epw_to_wea(self.epw_fp, self.wea_fp)
            self.epw.to_wea(str(self.wea_fp))
            logger.info("Converting WEA to MTX...")
            res = pr.gendaymtx(
                self.wea_fp,
                verbose=True,
                average=False,
                mfactor=mfactor,
                rotate=270,
                sky_color=[1, 1, 1],
                solar_radiance=True,
            )
            mtx = BytesIO(res)
            logger.info("Completed sky matrix extraction.")
            logger.info("Converting Reinhart to meridinal/parallel...")
            self.reinhart_to_meridinal_parallel_sky(mtx)
            logger.info("Converted Reinhart to meridinal/parallel.")
            del mtx

            self.dome_radius = dome_radius
            # self.render()

    def reinhart_to_meridinal_parallel_sky(self, mtx):
        """
        Converts a Tregenza sky matrix to a parallel/meridinal sky

        Methodology:
        - When subdividing a sky patch, assuming uniform radiance, the radiance does not change.
        - In a Tregenza/Reinhart sky, if there are n patches in a parallel band, and we want m patches,
            - find the lcm of (n,m)
            - this gives a multiplication factor k s.t. n*k = lcm(n,m)
            - subdivide each sky patch in the band into k pieces (essentially adding meridinal/azimuthal subdivisions)
            - now regroup the sky patches
            - now take the mean of the groups
            - this effectively handles the steradianal-weighted average without needing to track counts or solid angles
            - e.g. if you have 96 patches in a band, but you want 48, then clearly k=1, group by two, mean.
            - e.g. if you have 72 patches in a band, but you want 48, then clearly 1.5 patches will correspond to a new patch,
                - you could do 2/3 times one patch plus 1/3 times the other patch, but this gets annoying to track
                - you could use solid angles, but also annoying to track
                - easier to just go to the lcm of 144 - each of the patches gets divided into 2 patches, and then you group every 3 patches
                - take the means of the resulting groups to get the resulting radiance since they are all equally sized
            - e.g. if you have 24 patches in a band but you want 48, then clearly you just need to split patches in two
            - e.g. if you have 18 patches in a band, but you want 48, you go up to 144 by subdividing the 18 into 8 pieces each and grouping every 3
        - trivial to compute the solid angle of each patch
        - L = Radiance: W/sr/m2
        - Omega = Solid Angle: sr
        - E = Irradiance of normal surf: W/m2 = Integrate radiance over omega = L*Omega

        """
        df = pd.read_csv(mtx, skiprows=8, names=["R", "G", "B"], sep=" ")
        values = df.to_numpy()

        # sum rgb channels
        # TODO: track separately
        values: np.ndarray = values.mean(axis=1)

        # Standard tregenza sky division row sizes below.  gendaymtx's first "skypatch"
        # is the ground and the last is the zenith.
        base_patches_per_elevation = np.array(
            [30, 30, 24, 24, 18, 12, 6], dtype=np.int64
        )
        # reinhart subdivision adds parallels and meridians
        # store # of patches per parallel/elevation according to reinhart subdivision
        patches_per_elevation = (
            base_patches_per_elevation.repeat(self.mfactor) * self.mfactor
        )
        self.n_elevations = patches_per_elevation.shape[0]

        elevation_patches_end_ix = np.cumsum(patches_per_elevation)
        elevation_patches_start_ix = np.roll(elevation_patches_end_ix, shift=1)
        elevation_patches_start_ix[0] = 0

        # shape = n_skypatches x 8760
        sky_patch_timeseries = values.reshape(-1, 8760)
        assert np.sum(patches_per_elevation) == sky_patch_timeseries.shape[0] - 2
        sky_patch_timeseries = sky_patch_timeseries[
            1:-1
        ]  # remove the ground and the zenith

        # Convert reinhart sky to meridinal/parallel subdivision
        sky_patch_radiances = []
        for i in range(self.n_elevations):
            elevation_band_start = elevation_patches_start_ix[i]
            elevation_band_end = elevation_patches_end_ix[i]
            # Get the patches for the current parallel/elevation
            elevation_patches = sky_patch_timeseries[
                elevation_band_start:elevation_band_end
            ]
            # Check how many patches are in this elevation currently
            patches_in_elevation = elevation_patches.shape[0]
            # compute the common multiple of the current number of patches and desired number of patches
            lcm = np.lcm(patches_in_elevation, self.n_azimuths)
            # Figure out the subdivision factor - nb: no truncation error because lcm/patch_count is by def integer
            div_factor = int(lcm / patches_in_elevation)
            # figure out the grouping factor
            grouping_factor = int(lcm / self.n_azimuths)
            # radiance doesn't change when subdividing assuming uniform skypatch
            subdivided_patches = elevation_patches.repeat(div_factor, axis=0)
            # group the subdivided patches up so that the resulting outer dimension is the
            # number of target patches
            grouped_patches = subdivided_patches.reshape(
                self.n_azimuths, grouping_factor, 8760
            )
            # Combine patches by taking their mean, since they are all the same size
            # no solid angle weighting necessary
            resulting_patches = grouped_patches.mean(axis=1)
            sky_patch_radiances.append(resulting_patches)
        # Bands is now (n_elevations x n_azimuths x timesteps)
        self.radiance = np.stack(sky_patch_radiances)
        # zenith stays the same even after subdivision, so to find the distance
        # between elevational bands, we remove the zenith and then divide by the number of bands
        self.elevational_aperture = np.radians(90 - 6) / (self.n_elevations)
        # elevation starts are the lower bound of each parallel/latitudinal band
        self.elevation_starts_per_band = (
            np.arange(self.n_elevations).astype(float) * self.elevational_aperture
        )

        # Compute the elevation of the center for each parallel band (i.e. spherical zone)
        self.elevation_centers = (
            self.elevational_aperture * np.arange(self.n_elevations)
            + self.elevational_aperture / 2
        )
        # Compute the azimuth of the center for each meridinal band (i.e. a spherical lune)
        self.azimuth_centers = (
            self.azimuthal_aperture * np.arange(self.n_azimuths)
            + self.azimuthal_aperture / 2
        )

        # compute the solid angle of each sky patch - nb: every patch within a sky band has the same
        # solid angle as the other patches within the same band
        self.solid_angles = compute_quad_solid_angle(
            azimuthal_aperture=self.azimuthal_aperture,
            elevational_aperture=self.elevational_aperture,
            elevation_start=self.elevation_starts_per_band,
        )
        # irradiance of a normal surface is just: radiance of patch * solid angle
        self.normal_irradiance = self.radiance * self.solid_angles.reshape(-1, 1, 1)

        # Store it in a taichi field for gpu access later on
        self.normal_irradiance_field = ti.field(
            dtype=float, shape=self.normal_irradiance.shape
        )
        self.normal_irradiance_field.from_numpy(self.normal_irradiance)

    def init_rendering_fields(
        self,
        display_source: Literal["radiance", "normal irradiance"] = "radiance",
    ):
        if display_source == "radiance":
            color_source = self.radiance / 100
        else:
            color_source = self.normal_irradiance / 100

        self.color_source = ti.field(dtype=ti.f32, shape=color_source.shape)
        self.color_source.from_numpy(color_source)
        self.sky_pts: ti.math.vec3 = ti.Vector.field(
            3,
            dtype=ti.f32,
            shape=(self.n_elevations * self.n_azimuths),
        )
        self.sky_colors = ti.Vector.field(
            3,
            dtype=ti.f32,
            shape=(self.n_elevations * self.n_azimuths),
        )
        self.init_pts(125.0, 125.0)

    @ti.kernel
    def init_pts(self, x_offset: float, y_offset: float):
        for el_ix, az_ix in ti.ndrange(self.n_elevations, self.n_azimuths):
            pt_ix = el_ix * self.n_azimuths + az_ix
            # Centroid of sky patch's elevation
            el_angle = self.elevational_aperture * el_ix + self.elevational_aperture / 2
            az_angle = self.azimuthal_aperture * az_ix + self.azimuthal_aperture / 2
            pt_z = self.dome_radius * ti.sin(el_angle)
            r_proj = self.dome_radius * ti.cos(el_angle)
            pt_x = r_proj * ti.cos(az_angle)
            pt_y = r_proj * ti.sin(az_angle)
            self.sky_pts[pt_ix].x = pt_x + x_offset
            self.sky_pts[pt_ix].y = pt_z  # y axis is up in rendering
            self.sky_pts[pt_ix].z = pt_y + y_offset

    @ti.kernel
    def set_sky_colors(self, timestep: int):
        for el_ix, az_ix in ti.ndrange(self.n_elevations, self.n_azimuths):
            pt_ix = el_ix * self.n_azimuths + az_ix
            c = self.color_source[el_ix, az_ix, timestep]
            self.sky_colors[pt_ix].r = c
            self.sky_colors[pt_ix].g = c
            self.sky_colors[pt_ix].b = c

    def init_gui(self):
        self.window = ti.ui.Window("SkyDome Viewer", (1024, 1024), pos=(100, 100))
        self.gui = self.window.get_gui()
        self.canvas = self.window.get_canvas()
        self.scene = ti.ui.Scene()
        self.camera = ti.ui.Camera()
        self.camera.up(0, 1, 0)
        self.camera.position(0, 10, 0)
        self.camera.lookat(1, 10, 1)

    def add_dome_to_scene(self, scene=None):
        if scene == None:
            scene = self.scene
        scene.particles(
            self.sky_pts,
            radius=1,
            per_vertex_color=self.sky_colors,
        )

    def render(self):
        self.init_gui()
        self.init_rendering_fields()
        it = 0
        hr = 0
        while self.window.running:
            if it % 12 == 0:
                self.set_sky_colors(hr % 8760)
                ti.sync()
                hr = hr + 1
            self.camera.track_user_inputs(self.window, hold_key=ti.ui.RMB)
            self.scene.ambient_light((1, 1, 1))
            self.add_dome_to_scene()
            self.scene.set_camera(self.camera)
            self.canvas.scene(self.scene)
            it = it + 1
            self.window.show()


def compute_quad_solid_angle(azimuthal_aperture, elevational_aperture, elevation_start):
    """
    Compute the size of a sky patch in steradians

    Args:
        azimuthal (radians): angle between meridians/longitudinal lines
        elevational (radians): angle between parallels/latitude lines
        elevation (radians): elevation of lower parallel
    """

    # Compute the upper parallel's location
    elevation_top = elevation_start + elevational_aperture

    # regardless of where a lune starts or stops
    # any arc on a parallel/latitudnal line perp to the lune
    # is proportional to the azimuthal aperture of the lune
    lune_frac = azimuthal_aperture / (2 * np.pi)

    # the height of the zone is given by the difference in heights
    zone_height = np.sin(elevation_top) - np.sin(elevation_start)
    # zone area comes from integrals, and total surface area
    # of sphere is 4*pi
    # comes from i
    # zone_area = 2 * pi * zone_height
    zone_frac = zone_height / 2

    # The quadrilateral is just the product of these fractions
    # As it is an intersection of two independent events
    # quad frac is proportional area of a sphere
    quad_frac = lune_frac * zone_frac

    # 4*pi steradians in a sphere
    omega = quad_frac * 4 * np.pi

    return omega


if __name__ == "__main__":
    import os
    from pathlib import Path

    fp = Path("data") / "gis" / "Braga_Baseline.zip"
    height_col = "height (m)"
    id_col = "id"
    archetype_col = "Archetype"

    fp = Path("data") / "gis" / "Florianopolis_Baseline.zip"
    height_col = "HEIGHT"
    id_col = "OBJECTID"
    archetype_col = "BUILTYPE"

    epw_fp = (
        Path("data")
        / "epws"
        / "city_epws_indexed"
        / "cityidx_0000_USA_CA-Climate Zone 9.722880_CTZRV2.epw"
    )
    epw = EPW(epw_fp)
    sky = Sky(epw=epw, mfactor=4, n_azimuths=24, dome_radius=200)
    # sky.render()
    sky.init_rendering_fields()

    gdf = gpd.read_file(fp)
    tracer = Tracer(
        gdf=gdf,
        height_col=height_col,
        id_col=id_col,
        archetype_col=archetype_col,
        sensor_spacing=3,
        sky=sky,
    )

    tracer.ray_trace()
    ti.sync()
    tracer.print_column_stats(0)
    ti.sync()

    tracer.assemble_results_df()

    tracer.init_gui()
    tracer.render_scene(sky=True)
