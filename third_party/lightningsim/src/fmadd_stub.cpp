extern "C"
{
    // Vitis HLS emits these helper calls for floating-point fused multiply-add.
    // LightningSim only needs host-side linkage, so a plain multiply-add is
    // sufficient for trace generation.
    float _ssdm_op_FMADD(float a, float b, float c)
    {
        return (a * b) + c;
    }

    double _ssdm_op_FMADD_d(double a, double b, double c)
    {
        return (a * b) + c;
    }
}
