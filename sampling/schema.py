from functools import reduce

import numpy as np

from schedules import schedule_paths, operations


class SchemaParameter:
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
    )

    def __init__(
        self,
        name,
        shape_storage=(1,),
        shape_ml=(1,),
        target_object_key=None,
        dtype="scalar",
    ):
        self.name = name
        self.target_object_key = target_object_key
        self.dtype = dtype

        self.shape_storage = shape_storage
        self.shape_ml = shape_ml
        if shape_ml == (0,):
            self.in_ml = False

        self.len_storage = reduce(lambda a, b: a * b, shape_storage)
        self.len_ml = reduce(lambda a, b: a * b, shape_ml)

    def extract_storage_values(self, storage_vector):
        data = storage_vector[
            self.start_storage : self.start_storage + self.len_storage
        ]
        if self.shape_storage == (1,):
            return data[0]
        else:
            return data.reshape(*self.shape_storage)

    def extract_storage_values_batch(self, storage_batch):
        data = storage_batch[
            :, self.start_storage : self.start_storage + self.len_storage
        ]
        return data.reshape(-1, *self.shape_storage)

    def normalize(self, val):
        return val

    def unnormalize(self, val):
        return val

    def mutate_simulation_objects(self, epw, template, shoebox_dict):
        pass


class NumericParameter(SchemaParameter):
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


class ShoeboxOrientationParameter(OneHotParameter):
    __slots__ = ()

    def __init__(self, **kwargs):
        super().__init__(count=4, **kwargs)


class BuildingTemplateParameter(NumericParameter):
    __slots__ = "path"

    def __init__(self, path, **kwargs):
        super().__init__(**kwargs)
        self.path = path


class RValueParameter(BuildingTemplateParameter):
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)


class TMassParameter(BuildingTemplateParameter):
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)


class SchedulesParameters(SchemaParameter):
    __slots__ = ()
    paths = schedule_paths
    operations = operations

    def __init__(self, **kwargs):
        super().__init__(
            name="schedules",
            shape_storage=(len(schedule_paths), len(operations)),
            shape_ml=(len(schedule_paths), 8760),
            **kwargs,
        )


class Schema:
    __slots__ = ("parameters", "storage_vec_len", "ml_vec_len", "_key_ix_lookup")

    def __init__(self):
        self.parameters = [
            SchemaParameter(name="id", dtype="index", shape_ml=(0,)),
            SchemaParameter(name="base_template", dtype="index", shape_ml=(0,)),
            SchemaParameter(name="base_epw", dtype="index", shape_ml=(0,)),
            ShoeboxGeometryParameter(
                name="width",
                min=3,
                max=12,
                source="battini_shoeboxing_2023",
                help="Width of shoebox in meters",
            ),
            ShoeboxGeometryParameter(
                name="height",
                min=2.5,
                max=6,
                source="ComStock",
                help="Height of shoebox in meters",
            ),
            ShoeboxGeometryParameter(
                name="floor_2_facade",
                min=0.5,
                max=5,
                source="dogan_shoeboxer_2017",
                help="Ratio of adiabatic floor (length * floor width) to exterior wall area",
            ),
            ShoeboxGeometryParameter(
                name="core_2_perim",
                min=0,
                max=2,
                source="dogan_shoeboxer_2017",
                help="Ratio of perimeter area to core area (perim depth/(width-perim depth))",
            ),
            ShoeboxGeometryParameter(
                name="roof_2_floor",
                min=0,
                max=1.5,
                source="dogan_shoeboxer_2017",
                help="Ratio of roof (nnon-adiabatic) to adiabatic floor",
            ),
            ShoeboxGeometryParameter(
                name="ground_2_floor",
                min=0,
                max=1.5,
                source="dogan_shoeboxer_2017",
                help="Ratio of ground (nnon-adiabatic) to adiabatic floor",
            ),
            ShoeboxGeometryParameter(name="shading_fact", min=0, max=1),
            ShoeboxGeometryParameter(name="wwr_n", min=0, max=1),
            ShoeboxGeometryParameter(name="wwr_e", min=0, max=1),
            ShoeboxGeometryParameter(name="wwr_s", min=0, max=1),
            ShoeboxGeometryParameter(name="wwr_w", min=0, max=1),
            ShoeboxOrientationParameter(name="orientation"),
            BuildingTemplateParameter(
                name="LightingPowerDensity",
                path="Loads.LightingPowerDensity",
                min=0,
                max=20,
                source="ComStock",
                help="Lighting electricity consumption in W/m2",
            ),
            BuildingTemplateParameter(
                name="EquipmentPowerDensity",
                path="Loads.EquipmentPowerDensity",
                min=0.1,
                max=2150,  # TODO this is foor super high density spaces (like mech rooms). Alternative is 500
                source="ComStock",
                help="Electric equipment electricity consumption in W/m2",
            ),
            BuildingTemplateParameter(
                name="PeopleDensity",
                path="Loads.PeopleDensity",
                min=0,
                max=2,
                source="ComStock",
                help="Occupant density in people/m2",
            ),
            RValueParameter(
                name="FacadeRValue",
                path="Facade",
                min=0.1,
                max=15,
                source="ComStock, tacit knowledge",
                help="Exterior wall insulation R-value (m2K/W)",
            ),
            RValueParameter(
                name="RoofRValue",
                path="Roof",
                min=0.1,
                max=15,
                source="ComStock, tacit knowledge",
                help="Exterior roof insulation R-value (m2K/W)",
            ),
            RValueParameter(  # TODO: questionable if this is needed
                name="PartitionRValue",
                path="Partition",
                min=0.1,
                max=10,
                source="Tacit knowledge",
                help="Interior partition insulation R-value (m2K/W)",
            ),
            RValueParameter(
                name="SlabRValue",
                path="Slab",
                min=0.1,
                max=15,
                source="ComStock, tacit knowledge",
                help="Exterior slab insulation R-value - assuming no crawlspaces (m2K/W)",
            ),
            TMassParameter(
                name="FacadeMass",
                path="Facade",
                min=5,
                max=200,
                source="https://www.designingbuildings.co.uk/",
                help="Exterior wall thermal mass (J/Km2)",
            ),
            TMassParameter(
                name="RoofMass",
                path="Roof",
                min=5,
                max=200,
                source="https://www.designingbuildings.co.uk/",
                help="Exterior roof thermal mass (J/Km2)",
            ),
            TMassParameter(
                name="PartitionMass",
                path="Partition",
                min=5,
                max=100,
                source="https://www.designingbuildings.co.uk/, tacit",
                help="Interior partition thermal mass (J/Km2)",
            ),
            TMassParameter(
                name="SlabMass",
                path="Slab",
                min=5,
                max=200,
                source="https://www.designingbuildings.co.uk/",
                help="Exterior slab thermal mass (J/Km2)",
            ),
            SchemaParameter(
                name="schedules_seed",
                shape_ml=(0,),
                dtype="index",
            ),
            SchedulesParameters(),
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
        return list(self.parameters.keys())

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
    -- -1 i
    """
    schema.update_storage_batch(
        storage_batch, index=-1, parameter="FacadeRValue", value=2
    )
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
