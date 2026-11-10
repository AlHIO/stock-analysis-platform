from stockapp.modules.chip_analysis.domain.calculations import ChipAnalysisEngine, parameters
from stockapp.modules.chip_analysis.domain.models import AnalysisRequest, ChipAnalysisError
from stockapp.modules.chip_analysis.domain.ports import ChipCalculationEngine, ChipInputReader


class ChipAnalysisService:
    def __init__(self, reader: ChipInputReader, engine: ChipCalculationEngine | None = None):
        self.reader = reader
        self.engine = engine if engine is not None else ChipAnalysisEngine()

    def calculate(self, request: AnalysisRequest):
        # Reject unsupported parameters before invoking a future database reader.
        if not isinstance(request, AnalysisRequest):
            raise ChipAnalysisError('invalid_analysis_request')
        parameters(request)
        return self.engine.calculate(request, self.reader.load(request))
