 #!/usr/bin/env bash

if ! [ -f run_test.sh ]; then
    echo You are running in the wrong directory.
    exit 1
fi

export NOVA_CI_DATE=$(date +"%F-%H-%M-%S.%N")
R=$PWD/results/$DATE
mkdir -p $R
export NOVA_CI_LOG_DIR=$PWD/results/latest
ln -sf ${NOVA_CI_LOG_DIR} $R

export NOVA_CI_HOME=$HOME/nova-testscripts/nova-ci/
K_SUFFIX=nova

function get_kernel_version() {
    (
	cd $NOVA_CI_HOME/linux-nova; 
	make kernelversion
	)
}

export NOVA_CI_KERNEL_NAME=$(get_kernel_version)

function compute_grub_default() {
    KERNEL_VERSION=$(get_kernel_version)
    menu=$(grep 'menuentry ' /boot/grub/grub.cfg  | grep -n $KERNEL_VERSION-$K_SUFFIX | grep -v recovery | cut -f 1 -d :)
    menu=$[menu-2]
    echo "1>$menu"
}

function get_packages() {
    sudo apt-get -y update
    sudo apt-get -y build-dep linux-image-$(uname -r) fakeroot
    sudo apt-get -y install make gcc emacs
}

function update_kernel () {
    pushd $NOVA_CI_HOME
    git clone git@github.com:NVSL/linux-nova.git || (cd linux-nova; git pull)
    popd
}

function build_kernel () {
    pushd $NOVA_CI_HOME
    cp ../kernel/gce.config ./linux-nova/.config
    sudo rm -rf *.tar.gz *.dsc *.deb *.changes
    (
	set -v;
	cd linux-nova; 
	make -j9 deb-pkg LOCALVERSION=-${K_SUFFIX};
	) 2>&1 | tee $R/kernel_build.log 
    popd
    KERNEL_VERSION=$(get_kernel_version)
}

function install_kernel() {
    KERNEL_VERSION=$(get_kernel_version)
    pushd $NOVA_CI_HOME
    (
	set -v;
	cd $NOVA_CI_HOME;
	sudo dpkg -i   linux-image-${KERNEL_VERSION}-${K_SUFFIX}_${KERNEL_VERSION}-${K_SUFFIX}-?_amd64.deb &&
	sudo dpkg -i linux-headers-${KERNEL_VERSION}-${K_SUFFIX}_${KERNEL_VERSION}-${K_SUFFIX}-?_amd64.deb
	) || false
    sudo update-grub
}

function reboot_to_nova() {
    echo Rebooting to $(compute_grub_default)...
    sudo grub-reboot $(compute_grub_default)
    sudo systemctl reboot -i
}

function build_module() {
    pushd $NOVA_CI_HOME
    (set -v;
	cd linux-nova;
	make prepare
	make modules_prepare
	make SUBDIRS=scripts/mod
	make SUBDIRS=fs/nova
	cp fs/nova/nova.ko /lib/modules/${KERNEL_VERSION}/kernel/fs
	sudo depmod
	) |tee $R/module_build.log
    popd
    KERNEL_VERSION=$(get_kernel_version)
}

function build_and_reboot() {
    build_kernel
    if install_kernel; then
	reboot_to_nova
    else
	echo "Install failed"
    fi
}

function update_and_build_nova() {
    pushd $NOVA_CI_HOME
    if [ -d linux-nova ]; then
	cd linux-nova
	
	if git diff --name-only origin/master | grep -v fs/nova || 
	    ! [ -f /boot/vmlinuz-${KERNEL_VERSION}-* ]; then
	    echo Main kernel is out of date or missing
	    git diff --name-only origin/master
	    ls /boot/*
	    
	    git pull
	    build_and_reboot
	else
	    git pull
	    build_module
	fi
    else
	echo Linux sources missing
	git clone git@github.com:NVSL/linux-nova.git
	if [ -d linux-nova ]; then
	    cd linux-nova
	    build_and_reboot
	else
	    echo git failed
	fi
    fi
    popd 
}

function mount_nova() {
    true
}

(
    set -v
    get_packages
    update_and_build_nova
    mount_nova
) 2>&1 | tee  $R/run_test.log
