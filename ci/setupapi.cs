using System; using System.Runtime.InteropServices; using System.Text;
public static class Dev {
  [StructLayout(LayoutKind.Sequential)] public struct SP_DEVINFO_DATA { public int cbSize; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
  [DllImport("setupapi.dll", SetLastError=true)] public static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid g, IntPtr h);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiCreateDeviceInfoW(IntPtr s, string name, ref Guid g, string desc, IntPtr h, int flags, ref SP_DEVINFO_DATA d);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr s, ref SP_DEVINFO_DATA d, int prop, byte[] buf, int size);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiCallClassInstaller(int fn, IntPtr s, ref SP_DEVINFO_DATA d);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiDestroyDeviceInfoList(IntPtr s);
  [DllImport("newdev.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool UpdateDriverForPlugAndPlayDevicesW(IntPtr h, string hwid, string inf, int flags, out bool reboot);
  public static string Create(string hwid, string inf, string classGuid, string className) {
    Guid g = new Guid(classGuid);
    IntPtr set = SetupDiCreateDeviceInfoList(ref g, IntPtr.Zero);
    if (set == (IntPtr)(-1)) return "CreateDeviceInfoList failed " + Marshal.GetLastWin32Error();
    SP_DEVINFO_DATA d = new SP_DEVINFO_DATA(); d.cbSize = Marshal.SizeOf(d);
    if (!SetupDiCreateDeviceInfoW(set, className, ref g, null, IntPtr.Zero, 1, ref d)) return "CreateDeviceInfo failed " + Marshal.GetLastWin32Error();
    byte[] b = Encoding.Unicode.GetBytes(hwid + "\0\0");
    if (!SetupDiSetDeviceRegistryPropertyW(set, ref d, 1, b, b.Length)) return "SetHardwareId failed " + Marshal.GetLastWin32Error();
    if (!SetupDiCallClassInstaller(0x19, set, ref d)) return "RegisterDevice failed " + Marshal.GetLastWin32Error();
    bool reboot;
    if (!UpdateDriverForPlugAndPlayDevicesW(IntPtr.Zero, hwid, inf, 1, out reboot)) { int e = Marshal.GetLastWin32Error(); return "UpdateDriver failed " + e + " (0x" + e.ToString("x") + ")"; }
    SetupDiDestroyDeviceInfoList(set);
    return "ok reboot=" + reboot;
  }
}
