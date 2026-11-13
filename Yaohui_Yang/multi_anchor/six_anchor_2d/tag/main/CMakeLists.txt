idf_component_register(SRCS "app_main.c" INCLUDE_DIRS "."
    PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash lwip esp_timer esp_hw_support)
if(NOT DEFINED FTM_BURST_PERIOD)
    set(FTM_BURST_PERIOD 2)
endif()
if(NOT "${FTM_BURST_PERIOD}" MATCHES "^[0-2]$")
    message(FATAL_ERROR "FTM_BURST_PERIOD must be 0, 1 or 2")
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE FTM_BURST_PERIOD=${FTM_BURST_PERIOD})
