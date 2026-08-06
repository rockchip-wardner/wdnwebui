/* SWIG interface — exposes WdnddsStringTopicDataType + helpers to Python.

Build produces two files dropped into ``wdndds/dds/_native/``:

* ``_wdndds_string.so``   — the C++ extension
* ``wdndds_string.py``    — the SWIG-generated Python wrapper

SWIG can't fully parse FastDDS headers (``FASTDDS_EXPORTED_API`` macro
and nested class quirks), so we don't ``%import`` the upstream
``TopicDataType``. The C++ helper ``register_string_type`` does all
the FastDDS-typed work on the C++ side, so the Python caller never
has to deal with the broken ``fastdds.TypeSupport(TopicDataType *)``
SWIG overload.
*/

%module wdndds_string

%include "stdint.i"

%{
#include "WdnddsString.hpp"

#include <string>
%}

%include "../include/WdnddsString.hpp"

// C++ helpers — opaque-pointer dance because we don't %import
// DomainParticipant (SWIG can't parse it cleanly).
%inline %{
namespace wdndds {

const char* wdndds_register_string_type(
        void* participant,
        const char* alias)
{
    auto* p = static_cast<eprosima::fastdds::dds::DomainParticipant*>(participant);
    return wdndds::register_string_type(*p, std::string(alias));
}

void* wdndds_create_string_data()
{
    return wdndds::create_string_data();
}

void* wdndds_create_string_data_with(const char* value)
{
    auto* s = new std::string(value);
    return s;
}

void wdndds_delete_string_data(void* data)
{
    wdndds::delete_string_data(data);
}

}  // namespace wdndds
%}
