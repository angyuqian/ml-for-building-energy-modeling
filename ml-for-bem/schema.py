from functools import reduce

import numpy as np

from archetypal import UmiTemplateLibrary
from pyumi.shoeboxer.shoebox import ShoeBox
from schedules import schedule_paths, operations


class ShoeboxConfiguration:
    """
    Stateful class for shoebox object args
    """

    __slots__ = (
        "width",
        "height",
        "facade_2_footprint",
        "perim_2_footprint",
        "roof_2_footprint",
        "footprint_2_ground",
        "shading_fact",
        "wwr_n",
        "wwr_e",
        "wwr_s",
        "wwr_w",
        "orientation",
    )

    def __init__(self):
        pass


class WhiteboxSimulation:
    """
    Class for configuring a whitebox simulation from a storage vector
    """

    __slots__ = (
        "schema",
        "storage_vector",
        "template",
        "epw_path",
        "shoebox_config",
        "shoebox",
    )

    def __init__(self, schema, storage_vector):
        """
        Create a whitebox simulation object

        Args:
            schema: Schema, semantic method handler
            storage_vector: np.ndarray, shape=(len(storage_vector)), the storage vector to load
        Returns:
            A ready to simulate whitebox sim
        """
        self.schema = schema
        self.storage_vector = storage_vector
        self.shoebox_config = ShoeboxConfiguration()
        self.load_template()
        self.build_epw_path()
        self.update_parameters()

    def load_template(self):
        """
        Method for loading a template based off id in storage vector.
        """
        # TODO: for now defaulting to boston template library.
        template_lib = self.schema["base_template_lib"].extract_storage_values(
            self.storage_vector
        )

        template_id = self.schema["base_template"].extract_storage_values(
            self.storage_vector
        )

        # TODO: consider migrating away from independent loaders, but ensure there are no race conditions
        lib = UmiTemplateLibrary.open("./data/template_libs/BostonTemplateLibrary.json")
        self.template = lib.BuildingTemplates[int(template_id)]

    def update_parameters(self):
        """
        Method for mutating semantic simulation objects
        """
        for parameter in self.schema.parameters:
            parameter.mutate_simulation_object(self)

    def build_epw_path(self):
        """Method for building the epw path"""
        # TODO: implement, for now just defaults to montreal
        self.epw_path = "./data/epws/CAN_PQ_Montreal.Intl.AP.716270_CWEC.epw"

    def build_shoebox(self):
        """
        Method for constructing the actual shoebox simulation object
        """
        # TODO: implement wwr parser
        wwr_map = {0: 0, 90: 0, 180: 1, 270: 0}  # N is 0, E is 90
        # Convert to coords
        width = self.shoebox_config.width
        depth = self.shoebox_config.height / self.shoebox_config.facade_2_footprint
        perim_depth = depth * self.shoebox_config.perim_2_footprint
        height = self.shoebox_config.height
        zones_data = [
            {
                "name": "Perim",
                "coordinates": [
                    (width, 0),
                    (width, perim_depth),
                    (0, perim_depth),
                    (0, 0),
                ],
                "height": height,
                "num_stories": 1,
                "zoning": "by_storey",
            },
            {
                "name": "Core",
                "coordinates": [
                    (width, perim_depth),
                    (width, depth),
                    (0, depth),
                    (0, perim_depth),
                ],
                "height": height,
                "num_stories": 1,
                "zoning": "by_storey",
            },
        ]

        sb = ShoeBox.from_template(
            building_template=self.template,
            zones_data=zones_data,
            wwr_map=wwr_map,
        )
        sb.epw = self.epw_path

        # Set floor and roof geometry for each zone
        for surface in sb.getsurfaces(surface_type="roof"):
            name = surface.Name
            name = name.replace("Roof", "Ceiling")
            # sb.add_adiabatic_to_surface(surface, name, zone_params["roof_2_ground"])
        for surface in sb.getsurfaces(surface_type="floor"):
            name = surface.Name
            name = name.replace("Floor", "Int Floor")
            # sb.add_adiabatic_to_surface(
            #     surface, name, zone_params["footprint_2_ground"]
            # )
        # Internal partition and glazing
        # Orientation

        self.shoebox = sb


class SchemaParameter:
    """
    Base class for semantically representing operations on numpy/torch tensors
    which handles mutations of storage vectors, methods for updating simulation objects,
    and generating ML vectors from storage vectors, etc
    """

    __slots__ = (
        "name",
        "target_object_key",
        "dtype",
        "start_storage",
        "start_ml",
        "shape_storage",
        "shape_ml",
        "len_storage",
        "len_ml",
        "in_ml",
        "info",
    )

    def __init__(
        self,
        name,
        info,
        shape_storage=(1,),
        shape_ml=(1,),
        target_object_key=None,
        dtype="scalar",
    ):
        self.name = name
        self.info = info
        self.target_object_key = target_object_key
        self.dtype = dtype

        self.shape_storage = shape_storage
        self.shape_ml = shape_ml
        if shape_ml == (0,):
            self.in_ml = False

        self.len_storage = reduce(lambda a, b: a * b, shape_storage)
        self.len_ml = reduce(lambda a, b: a * b, shape_ml)

    def __repr__(self):
        return f"---{self.name}---\nshape_storage={self.shape_storage}, shape_ml={self.shape_ml}, dtype={self.dtype}\n{self.info}"

    def extract_storage_values(self, storage_vector):
        """
        Extract data values for this parameter from the current storage vector.  If this parameter represents matrix data,
        the data will be reshaped into the appropriate shape.
        Args:
            storage_vector: np.ndarray, shape=(len(storage_vector)) to extract data from
        Returns:
            data: float or np.ndarray, shape=(*parameter.shape), data associated with this parameter
        """
        data = storage_vector[
            self.start_storage : self.start_storage + self.len_storage
        ]
        if self.shape_storage == (1,):
            return data[0]
        else:
            return data.reshape(*self.shape_storage)

    def extract_storage_values_batch(self, storage_batch):
        """
        Extract data values for this parameter from all vectors in a storage batch.  If this parameter represents matrix data,
        the data will be reshaped into the appropriate shape so possibly a tensor if the parameter stores matrix data).
        Args:
            storage_batch: np.ndarray, shape=(n_vectors_in_batch, len(storage_vector)) to extract data from
        Returns:
            data: np.ndarray, shape=(n_vectors_in_batch, *parameter.shape), data associated with this parameter for each vector in batch
        """
        data = storage_batch[
            :, self.start_storage : self.start_storage + self.len_storage
        ]
        return data.reshape(-1, *self.shape_storage)

    def normalize(self, val):
        """
        Normalize data according to the model's schema.  For base SchemaParameters, this method
        does nothing.  Descendents of this (e.g. numerics) which require normalization implement
        their own methods for normalization.
        Args:
            val: np.ndarray, data to normalize
        Returns:
            val: np.ndarray, normalized data
        """
        return val

    def unnormalize(self, val):
        """
        Unnormalize data according to the model's schema.  For base SchemaParameters, this method
        does nothing.  Descendents of this (e.g. numerics) which require normalization implement
        their own methods for unnormalization.
        Args:
            val: np.ndarray, data to unnormalize
        Returns:
            val: np.ndarray, unnormalized data
        """
        return val

    def mutate_simulation_object(self, whitebox_sim: WhiteboxSimulation):
        """
        This method updates the simulation objects (archetypal template, shoebox config)
        by extracting values for this parameter from the sim's storage vector and using this
        parameter's logic to update the appropriate objects.
        The default base SchemaParameter does nothing.  Children classes implement the appropriate
        semantic logic.
        Args:
            whitebox_sim: WhiteboxSimulation
        """
        pass


class NumericParameter(SchemaParameter):
    """
    Numeric parameters which have mins/maxs/ranges can inherit this class in order
    to gain the ability to normalize/unnormalize
    """

    __slots__ = ("min", "max", "range")

    def __init__(self, min=0, max=1, **kwargs):
        super().__init__(**kwargs)
        self.min = min
        self.max = max
        self.range = self.max - self.min

    def normalize(self, value):
        return (value - self.min) / self.range

    def unnormalize(self, value):
        return value * self.range + self.min


class OneHotParameter(SchemaParameter):
    __slots__ = "count"

    def __init__(self, count, **kwargs):
        super().__init__(dtype="onehot", shape_ml=(count,), **kwargs)
        self.count = count


class ShoeboxGeometryParameter(NumericParameter):
    __slots__ = ()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def mutate_simulation_object(self, whitebox_sim: WhiteboxSimulation):
        """
        This method updates the simulation objects (archetypal template, shoebox config)
        by extracting values for this parameter from the sim's storage vector and using this
        parameter's logic to update the appropriate objects.
        Updates whitebox simulation's shoebox configuration dictionary class.
        Args:
            whitebox_sim: WhiteboxSimulation
        """
        value = self.extract_storage_values(whitebox_sim.storage_vector)
        setattr(whitebox_sim.shoebox_config, self.name, value)


class ShoeboxOrientationParameter(OneHotParameter):
    __slots__ = ()

    def __init__(self, **kwargs):
        super().__init__(count=4, **kwargs)


class BuildingTemplateParameter(NumericParameter):
    __slots__ = "path"

    def __init__(self, path, **kwargs):
        super().__init__(**kwargs)
        self.path = path.split(".")

    def mutate_simulation_object(self, whitebox_sim: WhiteboxSimulation):
        """
        This method updates the simulation objects (archetypal template, shoebox config)
        by extracting values for this parameter from the sim's storage vector and using this
        parameter's logic to update the appropriate objects.
        Updates whitebox simulation's direct building template parameters.
        Args:
            whitebox_sim: WhiteboxSimulation
        """
        value = self.extract_storage_values(whitebox_sim.storage_vector)
        template_param = self.path[-1]
        for zone in ["Perimeter", "Core"]:
            path = [whitebox_sim.template, zone, *self.path]
            path = path[:-1]
            object_to_update = reduce(lambda a, b: a[b], path)
            setattr(object_to_update, template_param, value)


class RValueParameter(BuildingTemplateParameter):
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)

    def mutate_simulation_object(self, whitebox_sim: WhiteboxSimulation):
        """
        TODO: Implement
        """
        pass


class TMassParameter(BuildingTemplateParameter):
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)

    def mutate_simulation_object(self, whitebox_sim: WhiteboxSimulation):
        """
        TODO: Implement
        """
        pass


class SchedulesParameters(SchemaParameter):
    __slots__ = ()
    paths = schedule_paths
    operations = operations

    def __init__(self, **kwargs):
        super().__init__(
            name="schedules",
            dtype="matrix",
            shape_storage=(len(schedule_paths), len(operations)),
            shape_ml=(len(schedule_paths), 8760),
            **kwargs,
        )


class Schema:
    __slots__ = ("parameters", "storage_vec_len", "ml_vec_len", "_key_ix_lookup")

    def __init__(self):
        self.parameters = [
            SchemaParameter(
                name="batch_id", dtype="index", shape_ml=(0,), info="batch_id of design"
            ),
            SchemaParameter(
                name="variation_id",
                dtype="index",
                shape_ml=(0,),
                info="variation_id of design",
            ),
            SchemaParameter(
                name="base_template_lib",
                dtype="index",
                shape_ml=(0,),
                info="Lookup index of template library to use.",
            ),
            SchemaParameter(
                name="base_template",
                dtype="index",
                shape_ml=(0,),
                info="Lookup index of template to use.",
            ),
            SchemaParameter(
                name="base_epw",
                dtype="index",
                shape_ml=(0,),
                info="Lookup index of EPW file to use.",
            ),
            ShoeboxGeometryParameter(
                name="width",
                min=3,
                max=12,
                source="battini_shoeboxing_2023",
                info="Width [m]",
            ),
            ShoeboxGeometryParameter(
                name="height",
                min=2.5,
                max=6,
                source="ComStock",
                info="Height [m]",
            ),
            ShoeboxGeometryParameter(
                name="facade_2_footprint",
                min=0.5,
                max=5,
                source="dogan_shoeboxer_2017",
                info="Facade to footprint ratio (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="perim_2_footprint",
                min=0,
                max=2,
                source="dogan_shoeboxer_2017",
                info="Perimeter to footprint ratio (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="roof_2_footprint",
                min=0,
                max=1.5,
                source="dogan_shoeboxer_2017",
                info="Roof to footprint ratio (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="footprint_2_ground",
                min=0,
                max=1.5,
                source="dogan_shoeboxer_2017",
                info="Footprint to ground ratio (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="shading_fact",
                min=0,
                max=1,
                info="Shading fact (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="wwr_n",
                min=0,
                max=1,
                info="Window-to-wall Ratio, N (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="wwr_e",
                min=0,
                max=1,
                info="Window-to-wall Ratio, E (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="wwr_s",
                min=0,
                max=1,
                info="Window-to-wall Ratio, S (unitless)",
            ),
            ShoeboxGeometryParameter(
                name="wwr_w",
                min=0,
                max=1,
                info="Window-to-wall Ratio, W (unitless)",
            ),
            ShoeboxOrientationParameter(
                name="orientation",
                info="Shoebox Orientation",
            ),
            BuildingTemplateParameter(
                name="LightingPowerDensity",
                path="Loads.LightingPowerDensity",
                min=0,
                max=20,
                source="ComStock",
                info="Lighting Power Density [W/m2]",
            ),
            BuildingTemplateParameter(
                name="EquipmentPowerDensity",
                path="Loads.EquipmentPowerDensity",
                min=0.1,
                max=2150,  # TODO this is foor super high density spaces (like mech rooms). Alternative is 500
                source="ComStock",
                info="Equipment Power Density [W/m2]",
            ),
            BuildingTemplateParameter(
                name="PeopleDensity",
                path="Loads.PeopleDensity",
                min=0,
                max=2,
                source="ComStock",
                info="People Density [people/m2]",
            ),
            RValueParameter(
                name="FacadeRValue",
                path="Facade",
                min=0.1,
                max=15,
                source="ComStock, tacit knowledge",
                info="Facade R-value",
            ),
            RValueParameter(
                name="RoofRValue",
                path="Roof",
                min=0.1,
                max=15,
                source="ComStock, tacit knowledge",
                info="Roof R-value",
            ),
            RValueParameter(
                name="PartitionRValue",
                path="Partition",
                min=0.1,
                max=10,
                source="Tacit knowledge",
                info="Partition R-value",
            ),
            RValueParameter(
                name="SlabRValue",
                path="Slab",
                min=0.1,
                max=15,
                source="ComStock, tacit knowledge",
                info="Slab R-value",
            ),
            TMassParameter(
                name="FacadeMass",
                path="Facade",
                min=5,
                max=200,
                source="https://www.designingbuildings.co.uk/",
                info="Exterior wall thermal mass (J/Km2)",
            ),
            TMassParameter(
                name="RoofMass",
                path="Roof",
                min=5,
                max=200,
                source="https://www.designingbuildings.co.uk/",
                info="Exterior roof thermal mass (J/Km2)",
            ),
            TMassParameter(
                name="PartitionMass",
                path="Partition",
                min=5,
                max=100,
                source="https://www.designingbuildings.co.uk/, tacit",
                info="Interior partition thermal mass (J/Km2)",
            ),
            TMassParameter(
                name="SlabMass",
                path="Slab",
                min=5,
                max=200,
                source="https://www.designingbuildings.co.uk/",
                info="Exterior slab thermal mass (J/Km2)",
            ),
            SchemaParameter(
                name="schedules_seed",
                shape_ml=(0,),
                dtype="index",
                info="A seed to reliably reproduce schedules from the storage vector's schedule operations when generating ml vector",
            ),
            SchedulesParameters(
                info="A matrix in the storage vector with operations to apply to schedules; a matrix of timeseries in ml vector",
            ),
        ]
        self.storage_vec_len = 0
        self.ml_vec_len = 0
        self._key_ix_lookup = {}
        for i, parameter in enumerate(self.parameters):
            self._key_ix_lookup[parameter.name] = i
            parameter.start_storage = self.storage_vec_len
            parameter.start_ml = self.ml_vec_len
            self.storage_vec_len += parameter.len_storage
            self.ml_vec_len += parameter.len_ml

    @property
    def parameter_names(self):
        """Return a list of the named parameters in the schema"""
        return list(self._key_ix_lookup.keys())

    def __getitem__(self, key):
        """
        Args:
            key: str, name of parameter
        Returns:
            parameter: SchemaParameter

        """
        return self.parameters[self._key_ix_lookup[key]]

    def __str__(self):
        """Generate a summary of the storach schema"""
        desc = "-------- Schema --------"
        for parameter in self.parameters:
            desc += f"\n---- {parameter.name} ----"
            desc += f"\nshape storage: {parameter.shape_storage} / shape ml: {parameter.shape_ml}"
            desc += f"\nlocation storage: {parameter.start_storage}->{parameter.start_storage+parameter.len_storage} / location ml: {parameter.start_ml}->{parameter.start_ml+parameter.len_ml}"
            desc += f"\n"

        desc += f"\nTotal length of storage vectors: {self.storage_vec_len} / Total length of ml vectors: {self.ml_vec_len}"
        return desc

    def generate_empty_storage_vector(self):
        """
        Create a vector of zeros representing a blank storage vector

        Returns:
            storage_vector: np.ndarray, 1-dim, shape=(len(storage_vector))
        """
        return np.zeros(shape=self.storage_vec_len)

    def generate_empty_storage_batch(self, n):
        """
        Create a matrix of zeros representing a batch of blank storage vectors

        Args:
            n: number of vectors to initialize in batch
        Returns:
            storage_batch: np.ndarray, 2-dim, shape=(n_vectors_in_batch, len(storage_vector))
        """
        return np.zeros(shape=(n, self.storage_vec_len))

    def update_storage_vector(self, storage_vector, parameter, value):
        """
        Update a storage vector parameter with a value (or matrix which will be flattened)

        Args:
            storage_vector: np.ndarray, 1-dim, shape=(len(storage_vector))
            parameter: str, name of parameter to update
            value: np.ndarray | float, n-dim, will be flattened and stored in the storage vector
        """
        parameter = self[parameter]
        start = parameter.start_storage
        end = start + parameter.len_storage
        if isinstance(value, np.ndarray):
            storage_vector[start:end] = value.flatten()
        else:
            storage_vector[start] = value

    def update_storage_batch(
        self, storage_batch, index=None, parameter=None, value=None
    ):
        """
        Update a storage vector parameter within a batch of storage vectors with a new value (or matrix which will be flattened)

        Args:
            storage_batch: np.ndarray, 2-dim, shape=(n_vectors, len(storage_vector))
            index: int | tuple, which storage vector (or range of storage vectors) within the batch to update.  omit or use None if updating the full batch
            parameter: str, name of parameter to update
            value: np.ndarray | float, n-dim, will be flattened and stored in the storage vector
        """
        parameter = self[parameter]
        start = parameter.start_storage
        end = start + parameter.len_storage

        if isinstance(value, np.ndarray):
            value = value.reshape(-1, parameter.len_storage)

        if isinstance(index, tuple):
            start_ix = index[0]
            end_ix = index[1]
            storage_batch[start_ix:end_ix, start:end] = value
        else:
            if index == None:
                storage_batch[:, start:end] = value
            else:
                storage_batch[index, start:end] = value


class Model:
    __slots__ = ("design_vectors", "map")


if __name__ == "__main__":
    schema = Schema()
    print(schema)

    """Create a single empty storage vector"""
    storage_vector = schema.generate_empty_storage_vector()

    """Create a batch matrix of empty storage vectors"""
    batch_size = 20
    storage_batch = schema.generate_empty_storage_batch(batch_size)

    """
    Updating a storage batch with a constant parameter
    """
    schema.update_storage_batch(storage_batch, parameter="FacadeRValue", value=2)
    print(schema["FacadeRValue"].extract_storage_values_batch(storage_batch))

    """Updating a subset of a storage batch with random values"""
    start = 2
    n = 8
    end = start + n
    parameter = "PartitionRValue"
    shape = (n, *schema[parameter].shape_storage)
    values = np.random.rand(*shape)  # create a random sample with appropriate shape
    schema.update_storage_batch(
        storage_batch, index=(start, end), parameter=parameter, value=values
    )
    print(
        schema[parameter].extract_storage_values_batch(storage_batch)[
            start - 1 : end + 1
        ]
    )  # use [1:11] slice to see that the adjacentt cells are still zero

    """Updating an entire batch with random values"""
    parameter = "SlabRValue"
    n = batch_size
    shape = (n, *schema[parameter].shape_storage)
    values = np.random.rand(*shape)  # create a random sample with appropriate shape
    schema.update_storage_batch(storage_batch, parameter=parameter, value=values)
    print(schema[parameter].extract_storage_values_batch(storage_batch))