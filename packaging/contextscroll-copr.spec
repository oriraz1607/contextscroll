%global debug_package %{nil}
# COPR repackages the reproducible, attested release daemon. Preserve its
# exact bytes instead of applying architecture-dependent post-processing.
%global __os_install_post %{nil}

Name:           contextscroll
Version:        0.6.1
Release:        1%{?dist}
Summary:        Context-aware middle-click autoscrolling for Linux
License:        MIT
URL:            https://github.com/oriraz1607/contextscroll
Source0:        contextscroll-v%{version}-linux-x86_64.tar.gz
Source1:        contextscroll-v%{version}-linux-aarch64.tar.gz
ExclusiveArch:  x86_64 aarch64

BuildRequires:  glib2
Requires:       acl
Requires:       at-spi2-core
Requires:       glib2
Requires:       libX11
Requires:       python3
Requires:       python3-gobject
Requires:       systemd
Requires:       systemd-udev
Requires(pre):  shadow-utils

%description
ContextScroll provides context-aware middle-click autoscrolling on GNOME
Wayland and X11 while preserving native middle-click actions on links, tabs,
and controls.

%prep
%ifarch x86_64
%setup -q -n contextscroll-v%{version}-linux-x86_64
%endif
%ifarch aarch64
%setup -q -T -b 1 -n contextscroll-v%{version}-linux-aarch64
%endif

%build
# The release bundle contains the reproducibly built static daemon.

%install
install -D -m 0755 prebuilt/contextscroll %{buildroot}/usr/bin/contextscroll
install -D -m 0755 bin/contextscroll-context %{buildroot}/usr/bin/contextscroll-context
install -d -m 0755 %{buildroot}/usr/lib/contextscroll/contextscroll
install -m 0644 contextscroll/*.py %{buildroot}/usr/lib/contextscroll/contextscroll/
install -D -m 0755 scripts/set-extension-enabled.sh %{buildroot}/usr/lib/contextscroll/set-extension-enabled
install -D -m 0644 systemd/contextscroll.service %{buildroot}/usr/lib/systemd/system/contextscroll.service
install -D -m 0644 systemd/contextscroll-context.service %{buildroot}/usr/lib/systemd/user/contextscroll-context.service
install -D -m 0644 udev/99-contextscroll.rules %{buildroot}/usr/lib/udev/rules.d/99-contextscroll.rules
install -D -m 0644 config/contextscroll.conf %{buildroot}/etc/contextscroll.conf
install -D -m 0644 gnome-extension/metadata.json %{buildroot}/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/metadata.json
install -m 0644 gnome-extension/extension.js gnome-extension/prefs.js gnome-extension/rule-validation.js %{buildroot}/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/
# extension.js resolves cursor assets relative to the extension root.
install -m 0644 gnome-extension/icons/*.svg %{buildroot}/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/
install -D -m 0644 gnome-extension/schemas/org.contextscroll.gschema.xml %{buildroot}/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas/org.contextscroll.gschema.xml
glib-compile-schemas %{buildroot}/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas
install -D -m 0644 gnome-extension/schemas/org.contextscroll.gschema.xml %{buildroot}/usr/share/glib-2.0/schemas/org.contextscroll.gschema.xml

%pre
set -eu
if getent group contextscroll >/dev/null; then
    group_id=$(getent group contextscroll | cut -d: -f3)
    group_members=$(getent group contextscroll | cut -d: -f4)
    [ "$group_id" -lt 1000 ] && [ -z "$group_members" ] || exit 1
else
    groupadd --system contextscroll
fi
if getent passwd contextscroll >/dev/null; then
    account=$(getent passwd contextscroll)
    user_id=$(printf '%s\n' "$account" | cut -d: -f3)
    home=$(printf '%s\n' "$account" | cut -d: -f6)
    shell=$(printf '%s\n' "$account" | cut -d: -f7)
    [ "$user_id" -lt 1000 ] && [ "$home" = /nonexistent ] || exit 1
    [ "$(id -gn contextscroll)" = contextscroll ] || exit 1
    [ "$(id -Gn contextscroll)" = contextscroll ] || exit 1
    case "$shell" in */nologin|/bin/false) ;; *) exit 1 ;; esac
else
    useradd --system --gid contextscroll --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin \
        --comment 'ContextScroll input daemon' contextscroll
fi

%posttrans
/usr/bin/glib-compile-schemas /usr/share/glib-2.0/schemas >/dev/null 2>&1 || :
/usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
/usr/bin/udevadm trigger --action=change --subsystem-match=input --settle >/dev/null 2>&1 || :
/usr/bin/udevadm trigger --action=change --name-match=uinput --settle >/dev/null 2>&1 || :
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
/usr/bin/systemctl try-restart contextscroll.service >/dev/null 2>&1 || :

%preun
if [ "$1" -eq 0 ]; then
    /usr/bin/systemctl stop contextscroll.service >/dev/null 2>&1 || :
fi

%postun
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
/usr/bin/glib-compile-schemas /usr/share/glib-2.0/schemas >/dev/null 2>&1 || :
/usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :

%files
%license LICENSE
%doc README.md SECURITY.md SBOM.spdx.json RELEASE-METADATA
%config(noreplace) /etc/contextscroll.conf
/usr/bin/contextscroll
/usr/bin/contextscroll-context
%dir /usr/lib/contextscroll
%dir /usr/lib/contextscroll/contextscroll
/usr/lib/contextscroll/contextscroll/*.py
/usr/lib/contextscroll/set-extension-enabled
/usr/lib/systemd/system/contextscroll.service
/usr/lib/systemd/user/contextscroll-context.service
/usr/lib/udev/rules.d/99-contextscroll.rules
/usr/share/glib-2.0/schemas/org.contextscroll.gschema.xml
%dir /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/metadata.json
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/extension.js
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/prefs.js
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/rule-validation.js
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/autoscroll-cursor.svg
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/autoscroll-direction.svg
%dir /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas/org.contextscroll.gschema.xml
/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas/gschemas.compiled

%changelog
* Thu Sep 24 2026 oriraz1607 <153127309+oriraz1607@users.noreply.github.com> - 0.6.1-1
- Install GNOME cursor SVGs at the extension-root paths used at runtime.

* Sat Sep 19 2026 oriraz1607 <153127309+oriraz1607@users.noreply.github.com> - 0.6.0-1
- Add a COPR-compatible source RPM around the verified release bundles.
